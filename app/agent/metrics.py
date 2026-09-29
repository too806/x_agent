"""Agent 运行质量的只读聚合（Phase 5A.1 第五节）。

全部是纯函数：输入 RunState（可能来自 checkpoint），输出一个 pydantic 模型。
不读数据库、不写数据库、不调用任何外部服务，因此能脱离 Graph 与数据库单独测。

这些数字回答的是"决策循环本身健不健康"，**不是**"稿子写得好不好"。后者要人读，
前者用来发现行为异常——例如"研究了三轮还是不写稿""改稿两次仍不收敛"。

刻意**不新增数据库字段**：这里每一个数都能从已有的 RunState 与决策轨迹算出来。
唯一需要新增的是一个 state 字段 `critic_tally`（见 app/agent/state.py），
因为 `critic_results` 是每轮被整体覆盖的，历史结论事后反推不出来。
"""

from typing import Any, Mapping

from app.agent.budget import AgentBudget, counter, load_budget, steps_exhausted
from app.agent.schemas import AgentAction
from app.schemas import AgentQualityMetrics

# Critic 的三种结论，同时也是 critic_tally 的键。写成常量是为了让"指标漏了
# 一种结论"在测试里立刻暴露，而不是安静地少算一个数。
CRITIC_DECISIONS: tuple[str, ...] = ("accept", "revise", "reject")


def _tally(tally: Mapping[str, Any], key: str) -> int:
    """读一个累计值。缺失或类型不对一律当 0——与 budget.counter 同一套防御。

    tally 来自 checkpoint，可能被旧版本或人写坏；一个坏值不该让控制台 500。
    """
    value = tally.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def finish_reason(state: Mapping[str, Any]) -> str:
    """finish 那一步的理由原文。

    取**最后一条** finish，因为硬停路径上可能出现不止一次收尾记录。
    取不到就返回空串：一个没有理由的结束本身就是要被看到的信息，
    不该由这里编一个出来。
    """
    for entry in reversed(state.get("decision_trace") or []):
        if getattr(entry, "action", None) == AgentAction.FINISH.value:
            return getattr(entry, "reason", "") or ""
    return ""


def quality_metrics(
    state: Mapping[str, Any], budget: AgentBudget | None = None
) -> AgentQualityMetrics:
    """从一次运行的最终 state 算出质量指标。

    budget 可注入是为了测试（与 build_graph 的理由相同）；生产路径读
    config/agent.yaml。

    一个已知的取舍：`budget_exhausted` 是拿**当前配置**的 max_steps 与当时的
    step_count 比的。预算一旦被改小，历史 Run 的这个布尔值会跟着变。要彻底消除
    它得把当时的预算存进 state——为一个诊断用的布尔值付这个代价不划算，
    所以这里的选择是"可能偏保守"，而不是"看起来一定对"。
    """
    budget = budget or load_budget()
    tally = state.get("critic_tally") or {}

    return AgentQualityMetrics(
        total_steps=counter(state, "step_count"),
        research_calls=counter(state, "research_count"),
        revision_count=counter(state, "revision_count"),
        candidate_count=len(state.get("candidates") or []),
        critic_accept_count=_tally(tally, "accept"),
        critic_revise_count=_tally(tally, "revise"),
        critic_reject_count=_tally(tally, "reject"),
        budget_exhausted=steps_exhausted(state, budget),
        finish_reason=finish_reason(state),
    )
