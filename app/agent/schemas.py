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

    # --- Phase 8C：与用户目标的相关性（工作单第四节）---
    #
    # 三态，**不是布尔**：
    #   True  = 材料里真的出现了目标里的词
    #   False = 没有出现（这个候选不能再被选中，也不会被拿去研究）
    #   None  = 本次不判定（目标里没有可核对的话题词，或这是旧 checkpoint）
    #
    # `None` 与 `False` 必须分得开。合起来的话，"目标写得随便一点"会变成
    # "所有候选都不相关"，一次运行什么都发现不了——而那不是判定，是没判定
    # 被当成了否定结论。`budget.qualified_topics` 据此只拦 `False`。
    #
    # 判据只有材料正文（见 app/agent/relevance.py）：标题与描述是模型写的，
    # 让它们参与判定等于允许模型用一句话把无关材料说成相关。
    goal_relevance: bool | None = None
    #: 一句话说明判定依据（命中了哪些词 / 为什么没命中 / 为什么没判定）。
    #: 写进 tool summary 与 Planner 的 state 渲染——不写出来，"没选这个候选"
    #: 和"这个候选根本没被发现"在界面上是一样的。
    goal_relevance_note: str = ""


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


class ContextOrigin(str, Enum):
    """TopicContext 里那段背景**主要**是从哪来的。

    工作单第五节要求 Context 服从 Claim Boundary：外部事实 / 用户前提 / Agent 推断。
    三者不是可信度高低，而是**必须分开声明的来源**——用户说"今天关注美股"是前提，
    不是事实；模型自己把两件事连起来是推断，不是材料里的话。
    """

    #: 材料里查得到出处（必须给出 `source_ids`，由工具核验）
    FACT = "fact"
    #: 来自用户目标（GOAL）。**不需要出处，也不能拿它当论证**
    PREMISE = "premise"
    #: Agent 的合理推断。**必须标成推断**，正文里不许写成"事实是"
    INFERENCE = "inference"


class TopicContext(BaseModel):
    """话题的最小必要背景（工作单第三、六、九节）。

    ## 它解决的那个真实故障

    真机上出现过这样一份稿子：选题是"华尔街热议的新型云"，而草稿第一句是
    「2.2 万星的开源库，把行情、财报、资金流几千个接口免费开放」，
    接着直接跳到一个结论。作者（Agent）在**研究过程里**已经知道"新型云"指什么、
    这件事跟它什么关系；但**读者不知道**——读者第一句就被一个陌生的具体项目拦住，
    看不出这整篇在讲哪个话题。

    所以 Context 的任务只有一件：**把"研究过程中已经知道的"补成"读者进入这个话题
    所需要知道的那一点点"**。它不承担说服、不承担论证、不承担新颖性。

    ## 四个字段都是"最小必要"

    `subject`（在讲什么）/ `definition`（必要的概念解释）/ `why_now`（为什么现在
    值得看）/ `core_tension`（里面真正的矛盾是什么）。**不要求四个都填满**——
    工作单第六节明确允许空着，因为编一段背景比留白更糟。字段名刻意不照搬工作单
    的举例：工作单要的是这四类信息，不是这四个名字。

    ## 它不是 Angle，也不是 Evidence

    - **不是 Angle**（工作单第七节）：这里没有一个判断，没有一个可以被反驳的结论。
      写进 Context 的应该是"谁都得同意的话"——不同意的那部分属于 Angle。
    - **不是 Evidence**（工作单第四节）：事实编号是给论证用的，Context 是给读者
      进门用的。把一条事实搬进 Context 来当入口，就是工作单第十二节禁止的
      "证据劫持"。
    """

    #: 这个话题在讲什么。一句话，写给**没读过研究材料的人**。
    subject: str = ""
    #: 必要的概念解释（"新型云"这类说法指什么）。不需要解释的不写。
    definition: str = ""
    #: 为什么是现在。可以是材料里的时间点，也可以是用户目标里的关注点。
    why_now: str = ""
    #: 这个话题里真正的矛盾是什么（读者为什么该在意）。**不含本文的判断**。
    core_tension: str = ""
    #: 上面这段背景的主要来源。默认 `INFERENCE`——最保守的那档：
    #: 模型没有明确申报时，不许它冒充事实。
    origin: ContextOrigin = ContextOrigin.INFERENCE
    #: 只有 `origin=fact` 时才可能有值，且必须落在本次真实的 research_item id 上。
    #: 核验不上的由工具清空**并把 origin 降级为 inference**（见 tools/evidence.py）。
    source_ids: list[str] = Field(default_factory=list)


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
    # 这个话题**对陌生读者**的最小必要背景（见 `TopicContext`）。
    #
    # 容器是证据包，只因为两者出自同一次 LLM 调用（extract_evidence）——
    # **不是**因为 Context 是"又一条证据"。`format_evidence` 绝不印它：
    # Writer / Critic 拿到的是一段单独渲染、另一套契约的文本块。
    # 混进证据清单里，它立刻退化成"多了一条可以引用的事实"，
    # 而那正是工作单第四节要挡住的。
    #
    # 默认 None 而不是 `TopicContext()`：Phase 6 之前的 checkpoint、手写的
    # fixture 里都没有这一项，而"本次没有背景"与"背景是空的"必须分得开。
    context: TopicContext | None = None


class ViewpointStance(str, Enum):
    """一个**判断**的强度（工作单第十节）。

    刻意不叫 `Stance`：`app/style/schemas.py::StanceStrength` 已经占了这个词，
    而它指的是**表达**有多硬（一个写作参数），与"这个判断有多笃定"是两件事。
    名字一样而含义不同，在两张表都按类名登记进 SERDE 的仓库里是危险的。

    `stance` 是表达的强度，**不是**事实的真值。证据只支持到 cautious 的时候，
    Agent 仍然可以选择更保守，但不能更笃定——上界由 `viewpoint.py` 从证据推出。
    """

    STRONG = "strong"
    MODERATE = "moderate"
    CAUTIOUS = "cautious"
    UNCERTAIN = "uncertain"


class ClaimScope(str, Enum):
    """一个判断声称自己管到哪一步（工作单第二十八、二十九节）。

    这是**申报**不是分数：它不能加分，只能触发"声明范围超过证据范围"这个标志
    与随之而来的 stance 上限。所以"一律申报最窄的 scope"既拿不到好处，
    也盖不住正文里写出来的全称判断（`BREADTH_MARKERS` 读的是正文）。
    """

    FACT = "fact"  # 只是解释/复述一条观察
    CASE = "case"  # 某个具体案例、某次发布、某家公司
    SEGMENT = "segment"  # 某个细分领域、某类产品、某一小段时间
    INDUSTRY = "industry"  # 整个行业 / 一个市场
    SOCIETY = "society"  # 整个社会 / 一个时代


class ViewpointAssessment(BaseModel):
    """`app/agent/viewpoint.py` 对一个观点的确定性评估明细。

    **这里的每一个数字都由 Python 从真实观测算出，LLM 不参与**（工作单第十一、
    十二节）。每个数都是 `float | None`：`None` = 本次没有评估过（Phase 5B.3
    之前的 checkpoint、手写的 fixture、缺原始输入），与"评估出来是 0"必须分得开。

    嵌套模型**不登记**进 SERDE：它嵌在已登记模型的类型化字段里，会被
    `model_dump()` 内联（见 `app/db/checkpointer.py` 的登记规则）。
    """

    # ---- 分项（0–1）----
    evidence_support: float | None = Field(None, ge=0.0, le=1.0)
    #: 封顶前的值。单一来源时 `evidence_support` 会被压到上限，这里保留原值，
    #: 否则"被压过"和"本来就低"在报告里看起来一模一样。
    evidence_support_raw: float | None = Field(None, ge=0.0, le=1.0)
    source_diversity: float | None = Field(None, ge=0.0, le=1.0)
    counterpoint_awareness: float | None = Field(None, ge=0.0, le=1.0)
    #: 还有多少材料没有被任何事实覆盖。**不由模型自报决定**——见 viewpoint.py。
    unknown_pressure: float | None = Field(None, ge=0.0, le=1.0)
    specificity: float | None = Field(None, ge=0.0, le=1.0)
    #: 新颖性是**确定性判定**（是不是事实复述 / 与同批其他观点重复），
    #: 不是"模型觉得新"。模型只能通过 `restates_known_material` 把它**调低**。
    novelty: float | None = Field(None, ge=0.0, le=1.0)
    #: `novelty` 乘以证据门控后的值——评分里用的是它，所以两者都要能看见，
    #: 否则"新颖但没证据"为什么没拿到分将无从解释。
    newness_contribution: float | None = Field(None, ge=0.0, le=1.0)
    confidence_raw: float | None = Field(None, ge=0.0, le=1.0)
    viewpoint_score_raw: float | None = Field(None, ge=0.0, le=100.0)

    # ---- 计数（分母与分子都留着，理由才能被核对）----
    bound_facts: int = 0
    total_facts: int = 0
    bound_sources: int = 0
    bound_authors: int = 0
    #: 绑定了来源、但材料里没有作者名的条数。不进作者集合，只报数——
    #: 补一个 "unknown" 占位会把三位作者静默算成一位。
    missing_author_sources: int = 0
    declared_counterpoints: int = 0
    bound_counterpoints: int = 0
    total_materials: int = 0
    unreferenced_materials: int = 0

    # ---- 确定性痕迹 ----
    #: 正文里出现的全称量词，逐字回传，方便人核对误报。
    breadth_markers: list[str] = Field(default_factory=list)
    #: 正文里出现的动机/意图断言，逐字回传（工作单第六节）。命中不等于错，
    #: 触发的是"只能写成解读"那条条件性下界。
    motive_markers: list[str] = Field(default_factory=list)
    #: 断言的动机没有被独立来源托住。critic 的 accept 上界读它。
    motive_unsupported: bool = False
    #: 用了"全网/首创"这类**无法核验**的表述。不作为新颖性加分，只作参考信号。
    originality_claim: bool | None = None
    restates_fact: bool | None = None
    #: 与同批另一个观点近似重复时，两者都记同一个组名（不保一个杀一个——
    #: 那需要一个"与新颖性无关"的预排序，第一版不做）。
    duplicate_group: str = ""

    #: 上面每个数字是怎么来的，一句一条，用的是能在输入里核对的量。
    reasons: list[str] = Field(default_factory=list)


class AngleDraft(BaseModel):
    """LLM 对"一个可支撑的观点"的声明。**这里没有任何分数字段。**

    与 `TopicClusterDraft` 同一条理由：分数量的是"有多值得说"，而语言模型没有
    能力对"几条事实""几位作者""多少字重合"做算术；让它给一个数，得到的会是
    一个看起来很像分数的措辞。`confidence` / `viewpoint_score` / `novelty` /
    `specificity` 全部由 `app/agent/viewpoint.py` 从真实观测算出。

    模型负责四件事：**判断**（claim）、**推理步骤**（reasoning）、
    **自我反驳**（counterpoints / assumptions / risk）、**申报**
    （claim_scope / stance）。申报是声明不是分数。

    `claim_scope` 与 `stance` 都是 `| None` 而不是必填：缺字段不该让整批
    解析失败（同 `CriticResult` 的分项），但**也不给一个方便的默认值**——
    默认成 `CASE` 是白送的操纵空间，默认成 `FACT` 更糟。没申报就记没申报。
    """

    claim: str = Field(min_length=1)
    #: **推理层**：从事实推到 thesis 的那一步（工作单第六节）。
    #: 没有它，"判断"与"事实"之间就是一个无法检查的跳跃。
    reasoning: str = ""
    why_it_matters: str = ""
    #: 追溯链的第二跳：angle → fact_id。**不接受 source_ids**——
    #: 要求 Angle 直接引原始材料，等于允许它绕过证据直接引帖子，
    #: 那样 EvidencePack 就成了摆设。
    evidence_ids: list[str] = Field(default_factory=list)
    #: 这个判断在什么情况下会错。与 `counterpoints` 的分工：
    #: risk 是"我可能错在哪"，counterpoint 是"有谁/哪条证据持相反说法"。
    risk: str = ""
    counterpoints: list[str] = Field(default_factory=list)
    #: 反方也能绑证据。声明了却引不到真实 fact 的反方不算分（工作单第八节）。
    counterpoint_evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    implications: list[str] = Field(default_factory=list)
    claim_scope: ClaimScope | None = None
    stance: ViewpointStance | None = None
    #: 自述这个观点只是复述已知材料。**只能把 novelty 调低，不能调高**。
    restates_known_material: bool = False
    #: 新在哪（自由文本，给人看）。**不进任何分数**。
    novelty_note: str = ""


class AngleDraftList(BaseModel):
    """`generate_angles` 的解析目标。

    刻意**不**沿用 Phase 5A 的 `AngleList` 这个名字：那个名字已按类名登记进
    SERDE，而它其实从来没有作为 state 顶层值出现过（进 state 的是
    `angles: list[Angle]`），所以那条登记是死的——与 `TopicCandidateList`
    当年一样。名字留着而元素含义变了，会让将来任何一份含该名字的旧 payload
    静默还原成一组没有 id、没有评估的空壳。改名的代价是一次 import 更新。
    """

    angles: list[AngleDraft] = Field(default_factory=list)


class Angle(BaseModel):
    """一个可支撑的观点：判断 + 推理 + 依据 + 反方 + 边界。

    Phase 5B.3 就地升级，**类名与动作名不变**（`generate_angles` /
    `select_angle`、state 键 `angles` / `selected_angle` 全部沿用）。工作单
    第四节要求"最终定义以现有代码为准"，而审计结论是 `Angle.claim` 今天就已经
    是判断、不是方向——所以不需要第二个模型，缺的是这条判断的**边界与依据**。

    **Phase 5B.3 之前的 checkpoint 里 `assessment is None`**，那时的
    `confidence` 是模型自己给的数。Run Detail 会如实说明这一点；不要把它与
    确定性算出来的置信度混为一谈。
    """

    # ---- Phase 5A 既有字段：原名、原单位、原默认值，一律不动 ----
    angle_id: str = ""
    claim: str = Field(min_length=1)
    why_it_matters: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    risk: str = ""
    # 单位保持 0–1（不改成 0–100）：evals 里已有 `confidence >= 0.7` 这类判据，
    # 改单位会让那些判据静默失效。Phase 5B.3 起由 Python 算。
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # ---- Phase 5B.3：推理层与自我反驳（模型给文本，Python 不算分）----
    reasoning: str = ""
    counterpoints: list[str] = Field(default_factory=list)
    counterpoint_evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    implications: list[str] = Field(default_factory=list)
    novelty_note: str = ""
    #: 模型自述这个观点只是复述已知材料。**必须留在 Angle 上**，不能只活在
    #: 草稿里：`select_angle` 会用同一份打分函数复算一遍分数，如果这个声明
    #: 在草稿→Angle 的转换中丢了，复算出的 novelty 会比生成时高，
    #: state 里就会出现"存下来的分"与"排序依据的分"不一致。
    restates_known_material: bool = False

    # ---- Phase 5B.3：申报与判定 ----
    #: 生效的立场（已被证据上界钳过）。下游（Writer / Critic / 前端）只读这个。
    stance: ViewpointStance | None = None
    #: 模型原本申报的值。留着才量得出"模型有多经常超证据承诺"。
    stance_declared: ViewpointStance | None = None
    #: Python 从证据推出的上界。与 `stance` 分开存，理由同 `trend_score_raw`。
    max_stance: ViewpointStance | None = None
    claim_scope: ClaimScope | None = None
    evidence_scope: ClaimScope | None = None
    #: 声明范围超出证据范围（或正文出现全称量词而证据是局部的）。
    #: **只触发上限与参考信号，不扣分**——悄悄扣分的暗分无法复核。
    scope_exceeds_evidence: bool | None = None
    #: 模型**根本没有申报** `claim_scope`。与"申报了一个恰好合规的范围"是两件事：
    #: 前者说明边界这一环漏了，后者说明边界是清楚的。分开存才能让 RULE-023
    #: 把两者都复算一遍——只存在 `ViewpointScore` 里的话，它到不了 checkpoint，
    #: 复算就少比一项，而少比一项就是给那一项留了一条可以悄悄变的路。
    scope_declared_missing: bool | None = None

    # ---- Phase 5B.3：Python 算出来的 ----
    #: 0–100，排序用。与 `confidence` 是两个问题：confidence 答"站不站得住"，
    #: 它答"值不值得拿出来说"。两者都与 `trend_score` 无关（工作单第十三节）。
    viewpoint_score: float | None = Field(None, ge=0.0, le=100.0)
    #: None = 本次没有评估过（旧 checkpoint / 缺原始输入）。
    assessment: ViewpointAssessment | None = None


class Candidate(BaseModel):
    """一条候选正文。

    `label` 是候选的写法定位（鲜明观点型 / 事实与反差型 / 讨论与不同视角型，
    见 `app/agent/post_structure.py` 的 `CandidateRole`），不是标题：
    三条候选必须在**写法**上真的不同，否则"生成 3 个候选"只是把同一句话
    换了三次措辞，Critic 也就无从比较。

    `label` 是**给人和评估看**的：流程从不按它分支（分支只认 `candidate_id`
    与 `decision`），所以模型填错它不会让运行走岔，只会让报告难读。
    """

    candidate_id: str = ""
    label: str = ""
    content: str = Field(min_length=1)
    # 被 revise 过几次。0 = 初稿。
    revision: int = 0
    #: 这条正文是哪个观点写出来的（Angle.angle_id，由工具赋值、**不采信 LLM**）。
    #: 没有它，Critic 就无法判断"正文是否忠实于它该表达的那个观点"，
    #: 工作单第二十六节那条"Writer 不能偷偷改 thesis"也就无从检查。
    #: 默认空串：Phase 5B.3 之前的 checkpoint 与 fixture 没有这个字段。
    viewpoint_ref: str = ""


class CandidateList(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)


class CandidateDraft(BaseModel):
    """Revise 的 LLM 输出：只重写一条。"""

    label: str = ""
    content: str = Field(min_length=1)


class ViewpointFit(BaseModel):
    """Critic 对"这条正文有没有把那个观点说清楚、说过头"的分项评价。

    照 `StyleFit` 的先例：**每一项都是 `float | None`**。`None` = 这一项本次
    没评（模型漏字段、老 prompt、非 Agent 运行），与"评出来是 0"必须分得开——
    这正是 `StyleFit` 当初用 `float | None` 而不是 `0.0` 的理由。

    **刻意没有总分**（工作单第二十七节明禁"viewpoint_score = 9.2"）。
    `Angle.viewpoint_score` 是 Python 算的可复算分数；这里这几个是 LLM 的
    语义判断，两者同名会让人以为可以互相印证，实际上是两回事。

    嵌套模型**不登记** SERDE（嵌在类型化字段里，会被 `model_dump()` 内联）。
    """

    #: 正文有没有把那个判断说明白。
    viewpoint_clarity: float | None = Field(None, ge=0.0, le=10.0)
    #: 判断是否落在证据支持得住的范围内。
    viewpoint_support: float | None = Field(None, ge=0.0, le=10.0)
    #: 正文是否忠实于它 `viewpoint_ref` 指的那个观点（不是另一个、也没被换掉）。
    viewpoint_consistency: float | None = Field(None, ge=0.0, le=10.0)
    #: 有没有处理反方，还是只挑支持自己的说。
    counterpoint_awareness: float | None = Field(None, ge=0.0, le=10.0)
    #: **注意方向：越高越差**。证据范围 < 主张范围即 overclaim（工作单第二十八节）。
    overclaim_risk: float | None = Field(None, ge=0.0, le=10.0)


class ContextFit(BaseModel):
    """正文对**陌生读者**的可进入性（工作单第十、十四节）。

    与 `ViewpointFit` 并排的一栏，理由相同：独立成一组，才能和"观点对不对"分开看——
    一条正文完全可以观点正确、证据扎实，同时让读者根本进不去。

    ## 闸门：一个是非题，不是打分

    `reader_can_follow`：**一个不知道这个话题的人，读完前两句，能不能说出"这篇在
    讲什么"？** 能 = true，不能 = false；**false 直接是 P1**。

    为什么不用 0–10 分：分数里的"7 分"是给分的人自己校准的，没有任何东西规定几分
    算过关，于是一条真人读不懂的稿子可以拿到 7 分照常交付。bool 没有中间档，也就
    没有可校准的余地。这条是真机上一个真实故障的补丁——一条草稿把背景里的术语
    （"创作者收益规则""违规风险"）原样抄了一遍却一个都没解释，词汇重合度满分、
    `context_missing` 判无异常，LLM 也给了 7 分，而人第一眼根本不知道它在说什么。

    ## 三个参考分与方向

    - `context_missing`：**越高越差**。正文假设了读者已经知道背景（"众所周知"
      "业内都清楚"、或者直接从一个没解释过的具体项目/数字起步）。
    - `concept_jump`：**越高越差**。从一个概念跳到另一个，中间那步解释省了。
    - `evidence_hijacking`：**越高越差**（工作单第十二节）。把一条事实当成"话题的
      入口"——「有一个 2.2 万星的项目 ↓ 所以这就是新型云的入口」：读者被一条具体
      事实拦住，而这条事实与话题的关系全靠他自己猜。

    **判的是"读者进不进得来"，不是"词汇专不专业"**（工作单第十一节）：
    术语本身不是问题，没有交代才是问题。专业词用对了、读者照样能跟上，这一项就该高。

    嵌套模型**不登记**进 SERDE（同 `ViewpointFit`）：它嵌在一个已登记模型的类型化
    字段里，会被 `model_dump()` 内联。默认 `None` 而不是 `ContextFit()`：
    "没评"与"评分为 0"必须分得开。
    """

    # 唯一的闸门项：false 即 P1。默认 None（"没评"）而不是 False——
    # 老 checkpoint 与非 Agent 运行没有这一项，不能把"没评"读成"读不懂"。
    reader_can_follow: bool | None = None
    context_missing: float | None = Field(None, ge=0.0, le=10.0)
    concept_jump: float | None = Field(None, ge=0.0, le=10.0)
    evidence_hijacking: float | None = Field(None, ge=0.0, le=10.0)


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

    # --- Phase 5B.3：与目标观点的一致性（工作单第二十七节）---
    # 默认 None 而不是 `ViewpointFit()`：老 prompt / 非 Agent 运行没有这一项
    # 可判，而"没评"与"评分为 0"必须分得开。
    viewpoint_fit: ViewpointFit | None = None

    # --- Phase 8B 前置：陌生读者可进入性（工作单第十节）---
    # 单独一栏，与 viewpoint_fit 并排：观点对不对与读者进不进得来是两回事，
    # 合成一个分就再也分不开"这条观点站不住"和"这条观点没问题但没人读得懂"。
    context_fit: ContextFit | None = None

    # --- 逐条可见的传播面观察（工作单第六节）---
    #
    # 「这条有没有留下一个读者能接上的话头」是**单条**就判得了的，所以它在这
    # 一栏，和只在整批才判得了的 `CriticReport.candidate_distinctness` 分开。
    #
    # **越高越好**，与 `overclaim_risk`/`context_missing` 那些"越高越差"的项
    # 方向相反，所以名字里不带风险意味。
    #
    # 它**不是门禁**，也**不是爆款的预测**：10 分的意思是"这条读完会有人想
    # 补充一句自己的经验或反对意见"，不是"这条会火"。点赞与曝光受账号、时间、
    # 分发影响，把文案结构说成它们的原因是把相关性当因果（工作单第四十节）。
    # 所以它只出现在报告里，不参与 `decision`。
    #
    # 用 `float | None` 而同栏其它七项用 `float`：那七项是 5B.1 就在的字段，
    # 老 checkpoint 里它们是 0.0 有意义的"评过、得分 0"；这一项是本轮新增，
    # 老 checkpoint 里没有它，`None`（没评）与 0.0（评了、很差）必须分得开。
    discussion_potential: float | None = Field(None, ge=0.0, le=10.0)


class SearchAttempt(BaseModel):
    """一次检索尝试：用了什么检索词、取回几条材料。

    **只进 checkpoint，不落库**（同 `style_recipe` / `feedback_context`）：
    它是"这次运行问过什么"，不是业务资产。

    存在的理由只有一个：**"连续空结果"是只能从历史里看出来的一件事**。
    `research_results` 是累加的材料、`research_count` 是调用次数，
    两者都推不出"最近两次是不是都空"，而这件事正是
    `app/agent/query_broadening.py` 决定要不要加宽检索词的唯一依据。
    检索词此前只活在日志与 trace 的散文里（`decision.arguments` 不落库），
    读不出来，所以这里给它一个结构化字段。
    """

    query: str = ""
    result_count: int = 0


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

    1. 三条候选的差别是"写法"上的（鲜明观点 / 事实与反差 / 讨论与不同视角），
       分开评价时 Critic 看不到彼此，就无法回答"哪一条更适合这个角度"——
       而那正是生成三条的意义。
    2. 一条一评要把同样的证据包送进模型三次，最贵的一步做三遍。

    `focus_candidate_id` 指出"这一轮该动谁"：需要改稿时它是被改的那条，
    无需改稿时通常是得分最高的那条。流程只认它对应的那一条结果
    （state 里的 `critic_result`），整批结果留在 `critic_results` 里供人查看。
    """

    results: list[CriticResult] = Field(default_factory=list)
    focus_candidate_id: str = ""

    # --- 整批才判得了的两个观察（工作单第六节）---
    #
    # 「三条候选是不是真的走了三条路」只有在**看得到三条**的地方才判得出来。
    # 放进 `CriticReport` 而不是 `CriticResult`，正是这个理由：分开评时
    # 每条都"看起来没问题"，而三条其实是同一句话的三种措辞。
    #
    # **它不是门禁**。理由是改稿预算：改稿一次只能重写一条，把"三条太像"
    # 判成 P1 会让每一轮都判 revise，预算空转两轮之后照样交付，而质量并不更好。
    # 生成的差异由 Writer prompt 保证，这里只负责**让它可见**——
    # 控制台与离线评估都读得到，而不是靠读稿子时的感觉。
    #
    # `None` = 本次没评（旧 checkpoint、非 Agent 运行），与"评出来是 0 分"
    # 必须分得开——同 `StyleFit` 里全部用 `float | None` 的理由。
    candidate_distinctness: float | None = Field(None, ge=0.0, le=10.0)
    #: 一句话说明差异在哪里 / 差在哪里。空串表示本次没有可说的。
    distinctness_note: str = ""
