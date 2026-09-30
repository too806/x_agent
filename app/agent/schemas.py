"""Phase 5A Agent Brain 的结构化输出模型。

全部是**LLM 结构化输出的契约**。集中在一个模块里有一条具体理由：凡是出现在
`RunState` 里的类型，都必须在 checkpointer 的 SERDE 里显式登记
（`app/db/checkpointer.py`，msgpack 只反序列化已登记的类型）。放在一起，
登记清单就不会漏，而漏掉的后果是恢复 Run 时直接失败。

设计约束（工作单第四节）：**Planner 绝不用自由文本决定下一步**。
`AgentDecision.action` 是枚举，非法值在 `model_validate_json` 阶段就被拒——
与 `GeneratedDraft` 完全同一套处理方式。
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.style.schemas import StyleFit


class AgentAction(str, Enum):
    """Planner 能选的下一步。

    继承 `str` 是必要的：这个值要进 JSON（LLM 输出）、进 msgpack（checkpoint）、
    进 PostgreSQL（decision trace 的 action 列）。普通 Enum 在后两个地方会序列化成
    整数或报错。

    Phase 5B.1 把 `select_topic` 加了回来，推翻了 Phase 5A 那条"选题由
    `research_topic` 的 target 表达"的判断。改变的是前提取材方式：现在
    `discover_topics` 产出的是**带确定性分数的候选集**，"选哪一个"是一个有
    分数可依据、事后要能复核的**独立判断**，而不是研究动作的附带参数。
    把它并进 `research_topic` 会让 decision trace 里看不出"当时为什么选它"，
    而 `select_topic` 那一步恰好是唯一能回答这个问题的记录。

    `finish` 之外的每个动作都对应一个 Tool（见 app/agent/tools/__init__.py）。
    """

    DISCOVER_TOPICS = "discover_topics"
    #: 在候选选题里选定一个。**不调 LLM**：分数已经由 trends.py 算好了，
    #: 再让模型"选一次"等于把同一个判断做两遍，还给了它推翻确定性排序的机会。
    SELECT_TOPIC = "select_topic"
    RESEARCH_TOPIC = "research_topic"
    DEEPEN_RESEARCH = "deepen_research"
    EXTRACT_EVIDENCE = "extract_evidence"
    GENERATE_ANGLES = "generate_angles"
    SELECT_ANGLE = "select_angle"
    GENERATE_CANDIDATES = "generate_candidates"
    CRITICIZE = "criticize"
    REVISE = "revise"
    FINISH = "finish"


class AgentDecision(BaseModel):
    """Planner 的一次决策。

    注意与 `app/models/agent_decision.py::AgentDecision`（ORM 行）同名但不同物：
    前者是"模型想做什么"，后者是"这一决策被记下来了"。两者刻意不互相 import——
    Planner 只产出前者，Repository 只写后者，落库由 Planner 节点在一处完成。
    """

    action: AgentAction
    # 必填且非空：decision trace 的价值几乎全在"为什么"上。
    # 允许空字符串等于允许一次没有理由的决策，那样 trace 就退化成动作流水。
    reason: str = Field(min_length=1)
    # 作用于哪个对象：topic 文本 / angle_id / candidate_id。
    target: str | None = None
    # 给 Tool 的附加参数（V1 几乎没有用到，但保留它的代价是一个字段，
    # 去掉它的代价是将来加参数时改 schema）。
    arguments: dict[str, Any] = Field(default_factory=dict)


class TopicLifecycle(str, Enum):
    """话题在本次 Run 生命周期里处在哪一步。

    继承 `str` 与 `AgentAction` 同理：这个值要进 msgpack（checkpoint）与 JSON，
    普通 Enum 在那些地方会变成整数。

    与 `TopicQualification` 分开是因为两者回答不同的问题：lifecycle 说"它现在
    处在哪一步"（流程位置），qualification 说"凭什么这么判"（判定理由）。
    合成一个枚举就必须在"某个话题既 stale 又被研究过"这类情形里二选一。
    """

    #: 发现过，但没达到"值得继续研究"的门槛。
    DISCOVERED = "discovered"
    #: 已经过了门槛，可以被 select_topic 选中。
    QUALIFIED = "qualified"
    #: 被 select_topic 选中，正在围绕它做研究。
    RESEARCHING = "researching"
    #: 太旧了。分档与分数分开：stale 是**事实**（时间），不是打分结果。
    STALE = "stale"
    #: 支撑结构太薄（单条帖子 / 单一作者），不再追。
    DISCARDED = "discarded"


class TopicQualification(str, Enum):
    """这个候选选题**值不值得继续研究**（Phase 5B.1 工作单第六节）。

    刻意把"有讨论"与"值得继续研究"分开：一条爆款帖子证明的是前者，
    而本阶段真正要判断的是后者。两者的判据不同（见 app/agent/trends.py::qualify），
    所以不能共用一个字段。
    """

    QUALIFIED = "qualified"
    #: 有讨论，但支撑太薄（作者数或可核验来源数不足）。**与分数无关**。
    WEAK_SUPPORT = "weak_support"
    #: 最新一条已经超出 stale 时限。
    STALE = "stale"
    #: 支撑结构够，但分数没到门槛。
    LOW_SIGNAL = "low_signal"


class TopicClusterDraft(BaseModel):
    """LLM 对"哪几条帖子在讲同一件事"的分组结果。

    **这里没有任何分数字段，这是刻意的。** 分数量的是"有多值得继续看"，
    而一个语言模型没有能力对"不同作者数""可核验引用比例"这类量做算术；
    让它给一个数，得到的会是一个看起来很像分数的措辞。
    分数一律由 `app/agent/trends.py` 从真实观测算出来（工作单第九节）。

    模型负责的三件事，恰好是它擅长的：**分组**（哪几条在讲同一件事）、
    **命名**（给这组起一个能被独立理解的标题）、**解释**（为什么是现在、
    还有什么不知道）。

    `source_ids` 必须逐字来自 prompt 里印出的 material id。工具会拿它去
    `research_item` 里核验：核验不上的**不丢弃、但要扣分**——见
    `trends.score_source_quality`。
    """

    title: str = Field(min_length=1)
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    why_now: str = ""
    unknowns: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class TopicClusterDraftList(BaseModel):
    """Discover 的 LLM 输出外壳（json_mode 要求顶层是对象）。"""

    topics: list[TopicClusterDraft] = Field(default_factory=list)


class CandidateTopic(BaseModel):
    """一个候选选题：LLM 的分组 + Python 的确定性打分。

    字段分三类，混在一起会看不清哪些能信：

    1. **模型给的**（title / description / keywords / why_now / unknowns）——
       是文本，不是判断依据。下游按分数排序，不按这些描述排序。
    2. **观测到的**（source_post_ids / first_seen_at / latest_seen_at /
       discussion_count / author_count / engagement_signal / observed_at）——
       全部来自本次扫描真实取回的材料，`engagement_signal` 是
       like/reply/repost/quote 的加权和，不是"热度"。
    3. **算出来的**（五项 sub-score + trend_score）——由 trends.py 计算，
       可复现、可解释。`trend_score` 是排序用的值，`trend_score_raw` 是
       封顶前的值（见 trends.py 的"单一来源上限"）。

    `source_post_ids` 指向本系统的 `research_item.id`，与 EvidenceFact.source_ids
    同一套句柄——两处都引同一个 id 空间，追溯链才不会在中间换一次尺子。
    """

    # 由工具赋值（t1、t2…，按 trend_score 降序重排后）。不采信模型给的值，
    # 与 fact_id / angle_id / candidate_id 同一套做法。
    topic_id: str = ""
    title: str = Field(min_length=1)
    description: str = ""
    keywords: list[str] = Field(default_factory=list)

    source_post_ids: list[str] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    latest_seen_at: datetime | None = None
    #: 本次扫描的观察时刻，同时是打分窗口的右端
    #: （左端 = observed_at − trends.TREND_WINDOW_HOURS）。存下来是为了
    #: **事后能复算**：没有它，novelty 的窗口位置就成了一个无法回溯的数。
    observed_at: datetime | None = None

    discussion_count: int = 0
    author_count: int = 0
    #: 观测到的加权互动量（原始值，未归一化）。
    engagement_signal: int = 0

    recency_score: float = 0.0
    spread_score: float = 0.0
    novelty_score: float = 0.0
    source_quality_score: float = 0.0
    engagement_score: float = 0.0

    trend_score: float = 0.0
    trend_score_raw: float = 0.0
    #: 每个分项的分子分母，形如"3 位不同作者参与，spread 0.7500（满值 4 人）"。
    #: 由 Python 生成——"可解释"意味着人能拿着它回原始材料复核，而不是看到一句
    #: 模型写的"这个话题很有讨论价值"。
    trend_reasons: list[str] = Field(default_factory=list)

    why_now: str = ""
    unknowns: list[str] = Field(default_factory=list)

    qualification: TopicQualification = TopicQualification.LOW_SIGNAL
    lifecycle: TopicLifecycle = TopicLifecycle.DISCOVERED


# Phase 5A 的类名。历史 checkpoint 里的 candidate_topics 是**按类名**反序列化的
# （见 app/db/checkpointer.py），留着这个名字，那些 Run 恢复时才能拿到对象而不是
# 一个降级后的 dict。旧字段（topic / signal / confidence）不在新模型里，会被
# pydantic 忽略，`title` 落到默认值——即旧 Run 恢复后标题为空。这**只**影响
# 恢复一个已经跑完的旧 Run，所以选择"留一个别名"而不是"保留一份旧 schema"。
TopicCandidate = CandidateTopic


class EvidenceFact(BaseModel):
    """一条事实，以及它从哪来。

    `fact_id` **由工具赋值**（f1、f2…），不采信 LLM 给的值：让模型自己编 id
    会得到重复或跳号，而 Angle 要按 id 引用它们，引用错了整条证据链就断了。
    """

    fact_id: str = ""
    fact: str = Field(min_length=1)
    # 追溯链的第一跳：fact → research_item.id
    source_ids: list[str] = Field(default_factory=list)
    # None 表示材料里没写时间，**不是**"这件事没有时间"。
    timestamp: datetime | None = None


class EvidencePack(BaseModel):
    """证据包：观点只能长在这上面，不能长在"帖子印象"上（工作单第九节）。"""

    facts: list[EvidenceFact] = Field(default_factory=list)
    # 材料之间互相印证的说法
    consensus: list[str] = Field(default_factory=list)
    # 与主流说法相左的说法——观点型内容的张力通常来自这里
    counterpoints: list[str] = Field(default_factory=list)
    # 查不到、或材料互相矛盾的地方。显式留白比装作知道更有价值：
    # 它正是 Planner 决定"要不要再研究一轮"的依据。
    unknowns: list[str] = Field(default_factory=list)
    # 整包里出现过的 research_item id 并集（由工具汇总，不采信 LLM）
    source_ids: list[str] = Field(default_factory=list)


class Angle(BaseModel):
    """一个可支撑的论点。"""

    angle_id: str = ""
    claim: str = Field(min_length=1)
    why_it_matters: str = ""
    # 追溯链的第二跳：angle → fact_id。**不接受 source_ids**——
    # 要求 Angle 直接引原始材料，等于允许它绕过证据直接引帖子，
    # 那样 EvidencePack 就成了摆设。
    evidence_ids: list[str] = Field(default_factory=list)
    risk: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class AngleList(BaseModel):
    angles: list[Angle] = Field(default_factory=list)


class Candidate(BaseModel):
    """一条候选正文。

    `label` 是候选的写法定位（直接观点 / 信息密集 / 反共识），不是标题：
    三条候选必须在**写法**上真的不同，否则"生成 3 个候选"只是把同一句话
    换了三次措辞，Critic 也就无从比较。
    """

    candidate_id: str = ""
    label: str = ""
    content: str = Field(min_length=1)
    # 被 revise 过几次。0 = 初稿。
    revision: int = 0


class CandidateList(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)


class CandidateDraft(BaseModel):
    """Revise 的 LLM 输出：只重写一条。"""

    label: str = ""
    content: str = Field(min_length=1)


class CriticResult(BaseModel):
    """Critic 对某一轮候选的结构化评价。

    七项分项检查给**默认值**是刻意的：真正决定流程的是 `score` 与 `decision`，
    让某个分项缺失（模型偶尔漏字段）升级成整轮失败，代价远大于收益。
    但 `decision` 与 `candidate_id` 必须由 LLM 明确给出——那是路由的依据。
    """

    candidate_id: str
    score: float = Field(0.0, ge=0.0, le=10.0)
    flags: list[str] = Field(default_factory=list)
    # 给 Writer 的具体修改指令。decision=revise 却没有它，revise 就只是重掷骰子。
    revision_targets: list[str] = Field(default_factory=list)
    decision: Literal["accept", "revise", "reject"]

    # --- 七项分项检查（工作单第十二节）---
    factual_support: float = Field(0.0, ge=0.0, le=10.0)
    stance_strength: float = Field(0.0, ge=0.0, le=10.0)
    specificity: float = Field(0.0, ge=0.0, le=10.0)
    # 注意方向：这一项**越高越差**（AI 味越重）
    generic_ai_style: float = Field(0.0, ge=0.0, le=10.0)
    hook_strength: float = Field(0.0, ge=0.0, le=10.0)
    persona_fit: float = Field(0.0, ge=0.0, le=10.0)
    evidence_binding: float = Field(0.0, ge=0.0, le=10.0)

    # --- Phase 5B.2：与目标 StyleRecipe 的一致性（工作单第二十三节）---
    # 判的是"是否符合本次的写作结构约束"，**不是**"像不像某个具体账号"
    # （工作单第二十三、三十二节的硬约束）。
    #
    # 嵌套 `StyleFit` **不需要**登记进 SERDE：它嵌在一个已登记模型的类型化
    # 字段里，会被 model_dump() 内联（见 app/db/checkpointer.py 的登记规则）。
    #
    # 默认 None 而不是 `StyleFit()`：没给 recipe 的运行（以及本次改动之前的
    # 历史 checkpoint）就没有这一项可判，而"没评"与"评分为 0"必须分得开——
    # 那正是 StyleFit 内部字段用 `float | None` 的理由。
    style_fit: StyleFit | None = None
    #: 与 `style_fit` 分开的一栏：结构是否符合 recipe.structure_type。
    #: 不并进 `style_fit` 是为了让"骨架不对"与"语气不对"在报告里能分开看——
    #: 前者通常要重写，后者改几句就行。
    structure_fit: float | None = Field(None, ge=0.0, le=10.0)


class DecisionTraceEntry(BaseModel):
    """Graph state 里的一条决策记录（step、动作、理由、结果）。

    与 `agent_decision` 表**故意有重叠**，各自解决不同的问题：

    - 表是权威的审计记录，带完整结果、能按 Run 查询，供控制台展示；
    - state 里这份让运行中的 Agent 能看到自己刚做过什么（prompt 要渲染它），
      而且 `aget_state` 就能读出整条时间线，不必为了调试再查一次库。

    两者的写入点在同一个地方（Planner 节点与统一 Tool 节点），因此不会漂移。
    """

    step: int
    action: str
    reason: str
    target: str | None = None
    result_summary: str = ""
    timestamp: datetime


def merge_trace(
    left: list[DecisionTraceEntry], right: list[DecisionTraceEntry]
) -> list[DecisionTraceEntry]:
    """`decision_trace` 的 reducer：按 step **覆盖**而不是追加。

    为什么不能用 `operator.add`：同一步先由 Planner 写入（只有意图），
    执行完再由 Tool 节点写入（补上结果）。追加的话每一步会出现两条——
    一条没有结果、一条没有理由，人得自己在脑子里配对。

    按 step 覆盖之后，整条轨迹每一步恰好一条，且始终是"最新已知"的那份。
    """
    by_step = {entry.step: entry for entry in left}
    for entry in right:
        by_step[entry.step] = entry
    return [by_step[step] for step in sorted(by_step)]


class CriticReport(BaseModel):
    """一次 Critic 调用对**整批**候选的评价。

    为什么是一批而不是一条一条评：

    1. 三条候选的差别是"写法"上的（直接观点 / 信息密集 / 反共识），
       分开评价时 Critic 看不到彼此，就无法回答"哪一条更适合这个角度"——
       而那正是生成三条的意义。
    2. 一条一评要把同样的证据包送进模型三次，最贵的一步做三遍。

    `focus_candidate_id` 指出"这一轮该动谁"：需要改稿时它是被改的那条，
    无需改稿时通常是得分最高的那条。流程只认它对应的那一条结果
    （state 里的 `critic_result`），整批结果留在 `critic_results` 里供人查看。
    """

    results: list[CriticResult] = Field(default_factory=list)
    focus_candidate_id: str = ""
