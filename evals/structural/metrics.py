"""结构指标：一次 Run 的行为骨架。

来源分三类，报告里每个数字旁边都会写清是哪一类（见 `source_of()`）：

1. **state 计数器**——`step_count` / `research_count` / `revision_count` / `candidates`。
   生产直接维护，是最权威的来源。
2. **trace 数出来的**——某种动作出现了几次。与 (1) 交叉核对；两者不一致时
   由 RULE-011 报出来，而不是在这里悄悄取其一。
3. **解析得到的**——`invalid_action_count`。见 `evals/analysis.py` 顶部关于
   `invalid`（模型原本想做什么）与 `executed_illegal_count`（最后执行了什么）
   为什么必须分开的说明。

`planner_steps` 取的是第 2 类的变体，定义需要说清楚，否则很容易变成一个和
`total_steps` 完全重复的字段：它是**经过 LLM 的步数**，即
`total_steps - deterministic_steps`。预算用尽后的收尾步（生产不再问模型，
直接按 `FALLBACK_ORDER` 走）不算 planner 步。这样
`planner_steps + deterministic_steps == total_steps` 恒成立，
而两者的比例恰好回答"这次运行有多少是模型在做决定"。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.agent.metrics import finish_reason as production_finish_reason
from evals.analysis import (
    METHOD_MARKER,
    METHOD_NONE,
    RunAnalysis,
    analyze,
    criticize_steps,
    research_steps,
)
from evals.model import AgentRunRecord
from evals.values import UNAVAILABLE, MetricValue, rate

#: 字段名清单。测试与报告遍历它来保证"加了字段不会漏渲染"。
STRUCTURAL_FIELDS: tuple[str, ...] = (
    "total_steps",
    "planner_steps",
    "deterministic_steps",
    "research_calls",
    "revision_count",
    "candidate_count",
    "critic_count",
    "critic_accept_count",
    "critic_revise_count",
    "critic_reject_count",
    "budget_exhausted",
    "hard_exhausted",
    "finish_reason",
    "invalid_action_count",
    "fallback_count",
    "unknown_action_count",
    "executed_illegal_count",
    "loop_repeat_count",
    "research_share",
    "critic_source",
    "invalid_source",
    "allowed_source",
)


class StructuralMetrics(BaseModel):
    """单次运行的骨架指标。

    `critic_*_count` 与 `invalid_action_count` 是 `int | str`：类型不允许它们
    被误当成确定值——不可用时就该是 `"unavailable"` 这个字符串。
    """

    model_config = ConfigDict(extra="forbid")

    # --- 步数 ---
    total_steps: int = 0
    planner_steps: int = 0
    deterministic_steps: int = 0

    # --- 各类调用 ---
    research_calls: int = 0
    revision_count: int = 0
    candidate_count: int = 0
    critic_count: int = 0

    # --- Critic 结论（累计）---
    critic_accept_count: int | str = UNAVAILABLE
    critic_revise_count: int | str = UNAVAILABLE
    critic_reject_count: int | str = UNAVAILABLE

    # --- 终止 ---
    budget_exhausted: bool = False
    hard_exhausted: bool = False
    finish_reason: str = ""

    # --- 异常行为 ---
    #: 模型提出了不可行动作的步数（回退标记数）。见 `invalid_source`。
    invalid_action_count: int | str = UNAVAILABLE
    #: 被回退替换的步数。当前与 `invalid_action_count` 同源同值，分开保留是因为
    #: 它们回答的问题不同（"模型判断错了" vs "决策权被收走了"），将来口径会分开。
    fallback_count: int = 0
    #: 最终执行的动作**不在**当时允许集合内的步数。正常应为 0。
    executed_illegal_count: int | str = UNAVAILABLE
    unknown_action_count: int = 0

    # --- 循环 ---
    #: 相邻两步动作相同的次数。这是一个粗筛，"无意义循环"的可靠判断要读 reason。
    loop_repeat_count: int = 0
    #: 研究类动作占总步数的比例。
    research_share: MetricValue = UNAVAILABLE

    # --- 来源标注 ---
    #: critic 结论是读的累计值还是只看到了最后一轮。
    critic_source: str = "critic_tally"
    #: invalid_action_count 的判定来源。恒为 marker：那是唯一能回答"模型原本
    #: 想做什么"的证据（见 evals/analysis.py）。空轨迹时为 none。
    invalid_source: str = METHOD_NONE
    #: executed_illegal_count 的判定来源：input_summary / replay / none。
    allowed_source: str = METHOD_NONE


def _critic_counts(analysis: RunAnalysis) -> tuple[int | str, int | str, int | str, str]:
    """Critic 三种结论的累计次数。

    `critic_tally` 是唯一可靠的来源：`critic_results` 每轮被整体覆盖，
    只能反映最后一轮。拿最后一轮当"整轮运行的结论分布"会把一次
    "先 revise 两轮、最后 accept" 的运行报成 "accept 100%"。

    tally 缺失时返回 UNAVAILABLE 并标注来源，**不**退回 critic_results 当替身——
    那正是上面那个错误。真要看最后一轮，另有专门的字段。
    """
    tally = analysis.record.state.critic_tally
    if tally is None:
        return UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, "missing"
    return (
        int(tally.get("accept", 0)),
        int(tally.get("revise", 0)),
        int(tally.get("reject", 0)),
        "critic_tally",
    )


def compute_structural(
    record: AgentRunRecord, analysis: RunAnalysis | None = None
) -> StructuralMetrics:
    """算出结构指标。可传入已算好的 analysis 以避免重复解析。"""
    analysis = analysis or analyze(record)
    state = record.state

    accept, revise, reject, critic_source = _critic_counts(analysis)

    # 空轨迹 = 没有决策可判，此时 UNAVAILABLE 才是诚实的。非空轨迹下回退标记
    # 是完备计数，可以直接报数。
    invalid: int | str = UNAVAILABLE if not analysis.steps else len(analysis.invalid_steps)
    invalid_source = METHOD_NONE if not analysis.steps else METHOD_MARKER

    if analysis.allowed_source == METHOD_NONE:
        executed_illegal: int | str = UNAVAILABLE
    else:
        executed_illegal = len(analysis.executed_illegal_steps)

    repeats = sum(
        1
        for previous, current in analysis.action_pairs
        if previous == current
    )

    return StructuralMetrics(
        total_steps=analysis.total_steps,
        planner_steps=analysis.total_steps - len(analysis.deterministic_steps),
        deterministic_steps=len(analysis.deterministic_steps),
        research_calls=state.research_count,
        revision_count=state.revision_count,
        candidate_count=len(state.candidates),
        critic_count=len(criticize_steps(analysis)),
        critic_accept_count=accept,
        critic_revise_count=revise,
        critic_reject_count=reject,
        budget_exhausted=analysis.budget_exhausted,
        hard_exhausted=analysis.hard_exhausted,
        # 直接用生产的实现：结束理由的取法（取最后一条 finish）已经有一套
        # 约定，评估自己再实现一遍只会分歧。这里只读，不改。
        finish_reason=production_finish_reason(_state_with_trace(record)),
        invalid_action_count=invalid,
        fallback_count=len(analysis.fallback_steps),
        executed_illegal_count=executed_illegal,
        unknown_action_count=len(analysis.unknown_action_steps),
        loop_repeat_count=repeats,
        research_share=rate(len(research_steps(analysis)), analysis.total_steps),
        critic_source=critic_source,
        invalid_source=invalid_source,
        allowed_source=analysis.allowed_source,
    )


def _state_with_trace(record: AgentRunRecord) -> dict:
    """给 `production_finish_reason` 一个它认得的形状。

    它读 `state["decision_trace"]` 里每条的 `.action` 与 `.reason`（属性访问），
    所以要传**带属性**的对象，不能传裸 dict。这里用最小的 shim，
    而不是把 `DecisionTraceEntry` 重新构造一遍——重构造会顺带要求 timestamp
    等评估并不关心的字段，凭空多一处与生产 schema 的耦合。
    """

    class _Entry:
        __slots__ = ("step", "action", "reason")

        def __init__(self, step: int, action: str, reason: str) -> None:
            self.step = step
            self.action = action
            self.reason = reason

    return {
        "decision_trace": [
            _Entry(e.step, e.action, e.reason) for e in record.trace
        ]
    }
