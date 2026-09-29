"""把一条 trace 拆成**逐步可判定的视图**——结构指标与行为规则的公共地基。

拆一次、用两处，是这里的全部理由。结构指标（"走了几步"）与行为规则
（"这一步合不合法"）读的是同一批事实。两边各解析一遍，就会出现
"步数是 12、非法动作数也是 12 但分母不同"这类对不上的报告。

## 两个不同的问题，必须分开判

这一步有**两个**关于"合不合法"的问题，它们看着像一回事，其实是两件事：

1. **模型原本想做的动作合法吗？** —— 这是 `invalid`，也是 RULE-010 要抓的东西。
2. **最终记下来的动作合法吗？** —— 这是 `executed_legal`。

第 1 个问题的答案**只能**来自回退标记。理由是：生产在换掉动作时一定写标记
（`app/agent/nodes/planner.py` 的 fallback 分支），所以"标记总数 = 被否决的
模型决策总数"，这是一个**完备**计数。反过来，没有标记只说明"没有被否决过"，
不需要别的证据。

第 2 个问题的答案来自 `input_summary` 里的 `allowed=` —— 那是生产在决策前
用真正的 `allowed_actions()` 算出来、原样写下的集合。它回答的是"这一步最终
执行的动作，在当时允许的集合里吗"。正常情况下永远是"在"（因为 Planner 自己
就会过滤），所以它主要是一道**数据自洽**检查：不成立说明 trace 被改过、
或者评估用错了当时的预算。

**把两者混起来是个陷阱**（本模块第一版就踩了）：如果拿 `allowed=` 去判 RULE-010，
那么每一次回退都会被判成"合法"——因为回退后的动作当然合法。于是这条规则
在所有数据上都通过，永远抓不到它本该抓的东西。所以两者各有各的字段。

## 第三条来源：state 快照重放

记录里带了 `steps` 时可以用生产的 `allowed_actions()` 重放。它服务的也是第 2 个
问题，且**要求快照是完整的**：`allowed_actions` 会把缺失的字段当空值处理，
一份只写了部分字段的快照会算出偏小（或偏大）的 allowed 集合，把合法动作
误判成非法。所以重放排在 `input_summary` 之后——后者是生产自己算的，
不依赖我们重建的 state 对不对。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agent.budget import (
    AgentBudget,
    allowed_actions,
    steps_exhausted,
    steps_hard_exhausted,
)
from app.agent.schemas import AgentAction
from evals.model import AgentRunRecord, AgentRunState
from evals.trace_marks import (
    is_deterministic_reason,
    is_hard_exhausted_reason,
    parse_allowed,
    parse_fallback,
)

#: 全部合法动作名。每步都比一遍会重复构造集合，且这是评估里最热的一行。
KNOWN_ACTIONS: frozenset[str] = frozenset(a.value for a in AgentAction)

#: 计入 research_count 的三个动作。与生产一一对应：
#: discover.py:60 / research.py:66 / research.py:106 都自增 research_count。
RESEARCH_ACTIONS: frozenset[str] = frozenset(
    {
        AgentAction.DISCOVER_TOPICS.value,
        AgentAction.RESEARCH_TOPIC.value,
        AgentAction.DEEPEN_RESEARCH.value,
    }
)

#: `executed_legal` 的判定来源，顺序即**可信度**顺序。
METHOD_SUMMARY = "input_summary"  # 读生产自己写下的 allowed= 列表（最权威）
METHOD_REPLAY = "replay"  # 用 allowed_actions() 重放 state 快照
METHOD_NONE = "none"  # 两条路都走不通 —— 结论是 unavailable

METHOD_PRIORITY: tuple[str, ...] = (METHOD_SUMMARY, METHOD_REPLAY, METHOD_NONE)

#: RULE-010 的判定来源。回退标记是唯一能回答"模型原本想做什么"的证据。
METHOD_MARKER = "marker"


class StepView(BaseModel):
    """一步的判定结果。"""

    model_config = ConfigDict(extra="forbid")

    step: int
    action: str
    reason: str = ""
    target: str | None = None
    result_summary: str = ""

    #: 动作名是不是一个已定义的 AgentAction。False = 轨迹里有未知动作。
    known: bool
    #: 这一步**没有**经过 LLM（预算收尾 / 强制结束）。
    deterministic: bool
    #: 收尾余量也用尽了，这是硬停。
    hard_stop: bool

    #: 模型原本想做的动作（仅当这一步被回退替换时有值）。
    fallback_from: str | None = None
    #: 实际执行的动作（回退时等于 action，仅作为对称性保留）。
    fallback_to: str | None = None

    #: **模型原本提议的动作被否决了**。判定来源只有回退标记。
    invalid: bool = False
    #: 最终执行的动作是否落在当时允许的集合内。None = 判不了。
    executed_legal: bool | None = None
    #: executed_legal 的来源。
    allowed_source: str = METHOD_NONE


class RunAnalysis(BaseModel):
    """一条 trace 的完整拆解。所有下游指标的**唯一**数据源。"""

    model_config = ConfigDict(extra="forbid")

    record: AgentRunRecord
    budget: AgentBudget
    steps: list[StepView] = Field(default_factory=list)

    #: 动作名 → 出现次数（只数已知动作）。
    action_counts: dict[str, int] = Field(default_factory=dict)
    #: 轨迹里的动作序列（原样，含未知动作）。
    action_sequence: list[str] = Field(default_factory=list)

    #: 模型提议被否决的步号（来自回退标记，完备计数）。
    invalid_steps: list[int] = Field(default_factory=list)
    #: 被确定性回退替换过的步号（当前与 invalid_steps 相同，分开保留是因为
    #: 将来若加入"预算收尾也算回退"的口径，两者会分开）。
    fallback_steps: list[int] = Field(default_factory=list)
    #: 未经 LLM 的步号。
    deterministic_steps: list[int] = Field(default_factory=list)
    #: 轨迹里出现了未定义动作的步号。
    unknown_action_steps: list[int] = Field(default_factory=list)
    #: 最终执行的动作**不在**当时允许集合内的步号。正常情况下应为空。
    executed_illegal_steps: list[int] = Field(default_factory=list)

    #: executed_legal 的整体来源：取用到的里面最弱的那一档。
    allowed_source: str = METHOD_NONE
    #: 每种来源各覆盖了几步。
    method_counts: dict[str, int] = Field(default_factory=dict)

    # --- 由生产预算函数直接得出的判定 ---
    budget_exhausted: bool = False
    hard_exhausted: bool = False

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def action_pairs(self) -> list[tuple[str, str]]:
        """相邻动作对，用于找循环。"""
        seq = self.action_sequence
        return list(zip(seq, seq[1:]))


def _normalize_snapshot(snapshot: dict) -> dict:
    """把一个裸 state 快照过一遍 `AgentRunState`，再取回 Mapping。

    为什么要绕这一圈：`allowed_actions()` 内部有 `getattr(state["evidence_pack"],
    "facts", None)` 这样的写法。快照直接从 JSON 读进来时，嵌套的是 dict，
    `getattr` 会失败并**静默**返回 None，于是 allowed 集合少一项，合法动作
    被判成非法。过一遍模型之后嵌套值是真正的生产类型，行为与 checkpoint 一致。
    """
    return AgentRunState(**snapshot).as_mapping()


def _replay_allowed(record: AgentRunRecord, budget: AgentBudget) -> list[bool | None]:
    """用 state 快照重放每一步的 allowed_actions()。

    约定：`record.steps[i]` 是第 i 步**决策之前** planner 看到的 state，
    与 `record.trace[i]` 一一对应。长度不齐时**整体放弃重放**而不是按位对齐——
    错位配对比不判定更糟，它会把"合法"说成"非法"（或反过来），而且看不出来。
    """
    if not record.steps or len(record.steps) != len(record.trace):
        return [None] * len(record.trace)

    allowed: list[bool | None] = []
    for snapshot, entry in zip(record.steps, record.trace):
        try:
            names = {a.value for a in allowed_actions(_normalize_snapshot(snapshot), budget)}
        except Exception:
            # 快照形状不对（缺字段、类型坏）时交给下一条来源，而不是让整条评估崩掉。
            names = None
        allowed.append(None if names is None else entry.action in names)
    return allowed


def analyze(record: AgentRunRecord, budget: AgentBudget | None = None) -> RunAnalysis:
    """把一条记录拆成 RunAnalysis。纯函数，无 IO。"""
    budget = budget or record.budget
    if budget is None:  # pragma: no cover - AgentRunRecord 的加载器总会填上
        raise ValueError("record.budget 为空：评估必须知道这次运行当时的预算")

    state = record.state
    replay = _replay_allowed(record, budget)

    steps: list[StepView] = []
    invalid_steps: list[int] = []
    fallback_steps: list[int] = []
    deterministic_steps: list[int] = []
    unknown_steps: list[int] = []
    executed_illegal: list[int] = []
    counts: dict[str, int] = {}
    sequence: list[str] = []
    method_counts: dict[str, int] = {}

    for index, entry in enumerate(record.trace):
        known = entry.action in KNOWN_ACTIONS
        if known:
            counts[entry.action] = counts.get(entry.action, 0) + 1
        else:
            unknown_steps.append(entry.step)
        sequence.append(entry.action)

        deterministic = is_deterministic_reason(entry.reason)
        if deterministic:
            deterministic_steps.append(entry.step)

        parsed = parse_fallback(entry.reason)
        fallback_from, fallback_to = parsed if parsed else (None, None)
        # 有回退标记 = 模型提议了一个被否决的动作。这是 RULE-010 的唯一判据。
        invalid = fallback_from is not None
        if invalid:
            invalid_steps.append(entry.step)
            fallback_steps.append(entry.step)

        # 执行动作是否合法：input_summary 优先，state 快照兜底。
        declared = parse_allowed(entry.input_summary)
        replayed = replay[index] if index < len(replay) else None
        if declared is not None:
            executed_legal: bool | None = entry.action in declared
            source = METHOD_SUMMARY
        elif replayed is not None:
            executed_legal = replayed
            source = METHOD_REPLAY
        else:
            executed_legal = None
            source = METHOD_NONE
        method_counts[source] = method_counts.get(source, 0) + 1
        if executed_legal is False:
            executed_illegal.append(entry.step)

        steps.append(
            StepView(
                step=entry.step,
                action=entry.action,
                reason=entry.reason,
                target=entry.target,
                result_summary=entry.result_summary,
                known=known,
                deterministic=deterministic,
                hard_stop=is_hard_exhausted_reason(entry.reason),
                fallback_from=fallback_from,
                fallback_to=fallback_to,
                invalid=invalid,
                executed_legal=executed_legal,
                allowed_source=source,
            )
        )

    # "整体来源"取**最弱**的那一档：只要有任何一步靠不住，就不能声称这个结论是
    # 完整的。宁可说"部分不可用"，也不要给一个看起来确定的错数——
    # "8 步全部可判定"和"8 步里 3 步可判定"在报告里必须长得不一样。
    weakest = METHOD_SUMMARY
    for method in METHOD_PRIORITY:
        if method_counts.get(method):
            weakest = method
    allowed_source = weakest if steps else METHOD_NONE

    return RunAnalysis(
        record=record,
        budget=budget,
        steps=steps,
        action_counts=counts,
        action_sequence=sequence,
        invalid_steps=invalid_steps,
        fallback_steps=fallback_steps,
        deterministic_steps=deterministic_steps,
        unknown_action_steps=unknown_steps,
        executed_illegal_steps=executed_illegal,
        allowed_source=allowed_source,
        method_counts=method_counts,
        # 用 as_mapping() 交给生产函数：它们全部按 Mapping 写。
        budget_exhausted=steps_exhausted(state.as_mapping(), budget),
        hard_exhausted=steps_hard_exhausted(state.as_mapping(), budget),
    )


def research_steps(analysis: RunAnalysis) -> list[StepView]:
    return [s for s in analysis.steps if s.action in RESEARCH_ACTIONS]


def revise_steps(analysis: RunAnalysis) -> list[StepView]:
    return [s for s in analysis.steps if s.action == AgentAction.REVISE.value]


def criticize_steps(analysis: RunAnalysis) -> list[StepView]:
    return [s for s in analysis.steps if s.action == AgentAction.CRITICIZE.value]
