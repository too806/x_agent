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


class AgentAction(str, Enum):
    """Planner 能选的下一步。

    继承 `str` 是必要的：这个值要进 JSON（LLM 输出）、进 msgpack（checkpoint）、
    进 PostgreSQL（decision trace 的 action 列）。普通 Enum 在后两个地方会序列化成
    整数或报错。

    刻意**不含** `select_topic`：选题由 `research_topic` 的 `target` 表达
    （"研究 Topic A"本身就意味着选定了 A），多一个纯记录动作只会让 trace 更啰嗦。
    """

    DISCOVER_TOPICS = "discover_topics"
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


class TopicCandidate(BaseModel):
    """Discover 阶段发现的一个候选选题。"""

    topic: str = Field(min_length=1)
    # 为什么是"现在"——时效性是选题质量的核心，缺了它候选就退化成关键词。
    why_now: str = ""
    signal: str = ""
    # 指向已落库的 research_item id。可能为空（模型判断不出出处时宁可留空，
    # 也不要编一个看起来像 id 的字符串）。
    source_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class TopicCandidateList(BaseModel):
    """Discover 的 LLM 输出外壳（json_mode 要求顶层是对象）。"""

    topics: list[TopicCandidate] = Field(default_factory=list)


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
