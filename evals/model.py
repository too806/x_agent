"""评估侧的输入模型：**一次 Run 的可评估快照**。

这个模块只做一件事——把"一次 Agent 运行"表述成一个 pydantic 模型，
并让它的每个结构化字段都**直接使用生产 schema 的类型**：

    from app.agent.schemas import Candidate, CriticResult, EvidencePack, ...

这是刻意的选择，而不是偷懒。评估工具最常见的死法，是自己抄一份 schema，
然后生产改了字段、评估还在按旧形状解析——于是所有指标静默变成默认值，
报告全绿，而实际什么都没测到。直接 import 生产类型之后，schema 一变，
fixture 在**加载阶段**就 pydantic ValidationError，红得很响。

## 与 RunState 的关系

`AgentRunState` 是 `app/agent/state.py::RunState` 的一个**子集 + 弱化**：

- 子集：只列评估真正要读的字段（V1 的 `research_results` 正文不读，
  只数条数）。
- 弱化：`RunState` 是 `total=False` 的 TypedDict，字段可以缺席；这里是
  BaseModel，缺席即默认值。于是评估代码不必到处 `state.get(x) or []`。
- `extra="allow"`：真实 checkpoint 里还有别的键（V1 字段、将来新增的），
  多出来的键**不该**让评估失败——但要留下痕迹，见 `from_state()` 的说明。

反过来，生产代码**不 import 本模块**。评估模型与业务 schema 之间是单向依赖，
所以评估口径的变化（加一个维度、换个算法）永远不需要动生产。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.budget import AgentBudget
from app.agent.schemas import (
    Angle,
    Candidate,
    CriticResult,
    EvidencePack,
    TopicCandidate,
)

__all__ = [
    "AgentRunRecord",
    "AgentRunState",
    "ContentQualityRecord",
    "DraftInfo",
    "Provenance",
    "RunOutcome",
    "TraceStep",
]

#: 数据来源。**必须**与 synthetic 一起出现，且 synthetic 为 True 时报告里会
#: 显式印出 "synthetic fixture"——评估报告最怕的是被人当成生产实测数据引用。
Provenance = Literal["fixture", "live-export", "recorded"]


class TraceStep(BaseModel):
    """`agent_decision` 表的一行 / `decision_trace` 的一条。

    两个来源的字段名不同，这里取**并集**，各自缺的那部分留默认值：

    - 数据库行（`app/models/agent_decision.py`）：
      step, action, reason, target, input_summary, result_summary, created_at
    - state 里的条目（`app/agent/schemas.py::DecisionTraceEntry`）：
      step, action, reason, target, result_summary, timestamp（**没有** input_summary）

    `extra="allow"` 收下 timestamp/created_at 这类双方不一致的字段：
    评估不读时间戳，但拒绝加载一条真实的 trace 只是因为多了个字段，
    代价远大于收益。
    """

    model_config = ConfigDict(extra="allow")

    step: int
    # 字符串而不是 AgentAction 枚举：轨迹里出现一个**非法**动作名恰恰是
    # RULE-001 要抓的东西。用枚举解析会在加载阶段就抛错，把"发现非法动作"
    # 变成"评估器崩溃"。非法值是数据，不是异常。
    action: str
    reason: str = ""
    target: str | None = None
    # 决策**之前**的状态摘要。真实 DB 行有，state 条目没有。
    input_summary: str = ""
    result_summary: str = ""


class AgentRunState(BaseModel):
    """RunState 里评估要读的那部分。

    字段名与 `app/agent/state.py::RunState` **逐一对齐**，值类型用生产模型。
    改这里的字段名等于改评估口径——不是重构，是换标尺。
    """

    model_config = ConfigDict(extra="allow")

    run_id: str = ""
    goal: str = ""
    topic: str = ""

    # --- 研究 ---
    candidate_topics: list[TopicCandidate] = Field(default_factory=list)
    selected_topic: str = ""
    # 只数条数，不读正文（正文可能很长，评估不需要）。
    research_item_ids: list[str] = Field(default_factory=list)

    # --- 推理 ---
    evidence_pack: EvidencePack | None = None
    angles: list[Angle] = Field(default_factory=list)
    selected_angle: Angle | None = None

    # --- 写作 ---
    candidates: list[Candidate] = Field(default_factory=list)

    # --- 评审 ---
    # critic_result 可以是 None：revise 工具改稿后会把它清空（见
    # app/agent/tools/revise.py 的 delta）。不设 None 会让真实的改稿后状态加载失败。
    critic_result: CriticResult | None = None
    critic_results: list[CriticResult] = Field(default_factory=list)
    # critic_tally 是**累计**结论（accept/revise/reject），唯一能恢复历史评审的字段：
    # critic_results 每轮被整体覆盖。缺失时为 None 而不是 {}——评估要靠这个区分
    # "这次运行没有任何评审"和"这份数据没有带 tally"，两者的结论完全不同。
    critic_tally: dict[str, int] | None = None

    # --- 计数 ---
    step_count: int = 0
    research_count: int = 0
    revision_count: int = 0
    writer_epoch: int = 0
    critic_epoch: int = 0

    # --- 下游（finalize 之后的兼容接缝）---
    draft_content: str = ""
    draft_id: str = ""
    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = Field(default_factory=list)
    human_action: str | None = None
    final_content: str = ""

    def as_mapping(self) -> dict[str, Any]:
        """转成生产函数认得的 Mapping。

        **必须**走这一步，不能直接把本对象交给 `app/agent/budget.py`：
        那边全部按 Mapping 写（`state.get(...)`），而 pydantic 模型没有 `.get`。
        同时也不能用 `model_dump()`——那会把 `EvidencePack` 之类的嵌套模型
        降级成 dict，而生产的 `has_evidence()` 是用 `getattr(pack, "facts")`
        读的，降级之后它会安静地返回 False，于是重放出来的 allowed 集合
        缺了 `generate_angles`，把合法动作误判成非法。这类错误不会报错，
        只会让 RULE-010 无端标红。

        返回值里的嵌套值是**真的生产模型对象**，与 checkpoint 里的形状一致。
        `extra="allow"` 收下的未知键一并带上：生产函数将来若开始读某个
        V1 字段，重放不该因为评估模型没声明它就给出不同的答案。
        """
        values: dict[str, Any] = dict(self.model_extra or {})
        for name in type(self).model_fields:
            values[name] = getattr(self, name)
        return values


class DraftInfo(BaseModel):
    """交付稿与人工改写稿。

    `human_edited_content` 存在时才能算 Human Edit Distance——它是内容质量
    评估里唯一**不能**从草稿本身算出来的指标（见 docs/agent-evaluation/content-quality.md）。
    """

    model_config = ConfigDict(extra="allow")

    draft_id: str = ""
    content: str = ""
    prompt_version: str = ""
    human_edited_content: str | None = None


class RunOutcome(BaseModel):
    """Run 的生命周期终点。

    这里的每个字段都必须来自**可观测事实**（数据库状态、提交的 review 记录），
    不接受"Agent 说自己做了"。评估的意义就在于独立于被评估者的自述。
    """

    model_config = ConfigDict(extra="allow")

    # agent_run.status：pending|running|waiting_for_review|approved|rejected|published|failed
    run_status: str = ""
    # 显式声明；为 None 时由 `human_review_reached()` 从 run_status / human_action 推导。
    human_review_reached: bool | None = None
    human_action: Literal["approve", "edit", "reject"] | None = None
    # None = 不知道（例如运行在 review 前就失败了），与 False 含义不同。
    published: bool | None = None
    # 谁发起的发布。**推导优先于采信**：只有 human_action ∈ {approve, edit} 才
    # 算人工发起，其余一律按自动发布处理。声明值只在与人证矛盾时用于加重告警。
    publish_initiated_by: Literal["human", "agent", "none"] = "none"
    error: str = ""


class ContentQualityRecord(BaseModel):
    """人工标注的内容质量字段（Phase 5A.1 **不做**自动填充）。

    八个维度刻意全部是 `float | None = None`：None 表示"没人标注过"，与 0.0
    （标注为最差）是两件完全不同的事。评估报告里前者显示 `unavailable`。

    `human_ai_style_rating` 与 `ai_style` 的自动指标并置，而不是互相替代：
    自动指标是**发现问题**的筛子，人工评分是**真值**。让 LLM 给自己打分
    会把两者混成一个既不可复现也不权威的数。
    """

    model_config = ConfigDict(extra="allow")

    # 1-5 分，None = 未标注
    #: 是否包含具体事实、数字、实体。
    specificity: float | None = None
    #: 是否有明确观点（而不是罗列）。
    stance: float | None = None
    #: 观点能否追溯到 evidence。
    evidence_binding: float | None = None
    #: 套话密度（越高越差）。
    generic_language: float | None = None
    #: 与"统一模板"的相似度（越高越差）。
    template_likeness: float | None = None
    #: 开头切入点是否具体。
    hook_strength: float | None = None
    #: 信息密度是否服务于判断（越高越好；纯堆砌反而低）。
    information_density: float | None = None
    #: 人工改写与 Agent 草稿的差异程度（0 = 一字未改）。**越高越差**。
    human_edit_distance: float | None = None
    #: 人工对"AI 味"的整体评分（越高越像 AI 写的）。这是真值，不由模型产生。
    human_ai_style_rating: float | None = None
    annotator: str = ""
    note: str = ""


class AgentRunRecord(BaseModel):
    """一次 Run 的完整可评估快照。评估器的唯一输入类型。

    评估器**只**认这一个入口，不直接读文件、不读数据库。这样"评估器支持什么"
    是一个可以被类型检查的问题，而不是散在各处的 `dict.get`。
    """

    model_config = ConfigDict(extra="allow")

    # --- 出处（必须诚实）---
    #: 人的可读标识，通常等于 fixture 文件名（无扩展名）。
    label: str
    #: True = 合成数据。报告里会原样印出，防止被误当成实测。
    synthetic: bool = True
    provenance: Provenance = "fixture"
    note: str = ""

    run_id: str = ""
    goal: str = ""
    #: 这次运行**当时**的预算。不填则回退到 config/agent.yaml 的当前值——
    #: 但那会引入 app/agent/metrics.py 已经记录过的同一个坑（历史 Run 的
    #: budget_exhausted 会随配置漂移），所以 fixture 一律显式声明。
    budget: AgentBudget | None = None

    state: AgentRunState = Field(default_factory=AgentRunState)
    trace: list[TraceStep] = Field(default_factory=list)
    outcome: RunOutcome = Field(default_factory=RunOutcome)
    draft: DraftInfo = Field(default_factory=DraftInfo)

    #: 逐步骤的 state 快照（可选）。有它就能**重放** `allowed_actions()`，
    #: 从而不依赖自然语言标记判断非法动作。没有就退化为解析 reason。
    steps: list[dict[str, Any]] | None = None

    #: 人工标注。fixture 里通常留空——本阶段的评估不假装有真值。
    content_quality: ContentQualityRecord = Field(default_factory=ContentQualityRecord)
