"""Planner：Agent 的大脑，负责**决定下一步做什么**。

三条边界，全部是工作单直接要求的：

1. **不写内容**（第五节）。Planner 的输出只有一个 `AgentDecision`，没有正文、
   没有证据、没有观点。写作与评判由各自的角色做——一个既能决策又能写作的 Planner,
   在出问题时无法区分"判断错了"还是"笔头错了"。
2. **不用自由文本决定路由**（第四节）。`action` 是枚举，非法值在
   `model_validate_json` 阶段就被拒，与 `GeneratedDraft` 同一套处理。
3. **预算不由模型自觉遵守**（第三节）。模型的决策先经 `allowed_actions` 过滤，
   不可行的会被**确定性地**换成回退动作。这一层在 LLM 之外，模型说什么都绕不过。

本节点是全流程唯一调用 Planner prompt 的地方，也是唯一写 `agent_decision` 表的
地方（结果由执行节点回填）。
"""

import logging
from datetime import datetime, timezone

from app.agent.budget import (
    WRAPUP_ALLOWANCE,
    AgentBudget,
    allowed_actions,
    counter,
    pick_fallback,
    steps_exhausted,
    steps_hard_exhausted,
)
from app.agent.prompts import (
    ACTION_HINTS,
    format_allowed,
    format_budget,
    format_state,
    format_trace,
    load_prompt,
    render,
)
from app.agent.schemas import AgentAction, AgentDecision, DecisionTraceEntry
from app.agent.tools.base import complete_json
from app.repositories import create_decision
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


def summarize_input(state, allowed: tuple[AgentAction, ...], budget: AgentBudget) -> str:
    """决策时"看到了什么"的摘要。

    **刻意不是完整 prompt**（工作单第十五节：不要存完整 prompt / secrets）。
    prompt 里混着研究材料原文与人格设定，既长又可能带敏感内容；而事后要看的是
    "当时手里有什么、允许做什么"，那正是这里列的这些数字与动作名。
    """
    pack = state.get("evidence_pack")
    return (
        f"allowed={','.join(a.value for a in allowed)}; "
        f"step={counter(state, 'step_count')}/{budget.max_steps}; "
        f"research={counter(state, 'research_count')}/{budget.max_research_calls}; "
        f"topics={len(state.get('candidate_topics') or [])}; "
        f"materials={len(state.get('research_item_ids') or [])}; "
        f"facts={len(getattr(pack, 'facts', None) or [])}; "
        f"angles={len(state.get('angles') or [])}; "
        f"candidates={len(state.get('candidates') or [])}; "
        f"revisions={counter(state, 'revision_count')}/{budget.max_revisions}"
    )


async def _decide(state, allowed, budget: AgentBudget, llm_service: LLMService) -> AgentDecision:
    """得到这一步的决策：预算内问模型，预算外走确定性回退。"""
    if steps_hard_exhausted(state, budget):
        # 连收尾余量都用完了。走到这里说明前面的收尾没能推进流程（例如模型持续
        # 产不出可用产物）。硬停，交给下游按"没有产出"处理——不能无限试下去。
        return AgentDecision(
            action=AgentAction.FINISH,
            reason=f"步数与收尾余量均已用尽（{budget.max_steps}+{WRAPUP_ALLOWANCE}），强制结束",
        )

    if steps_exhausted(state, budget):
        # 预算用尽时**不再调用 LLM**：既省一次调用，也避免给模型一个"其实没有选择"
        # 的决策机会（它的选择必然会被回退覆盖，那一步的 reason 就没有意义了）。
        action = pick_fallback(state, budget)
        return AgentDecision(
            action=action,
            reason=f"决策步数预算已用尽（{budget.max_steps} 步），按回退顺序收尾：{ACTION_HINTS.get(action, '')}",
        )

    prompt = render(
        load_prompt("planner"),
        GOAL=state.get("goal") or state.get("topic") or "",
        STATE=format_state(state),
        BUDGET=format_budget(state, budget),
        ALLOWED=format_allowed(allowed),
        TRACE=format_trace(state.get("decision_trace") or []),
    )
    decision: AgentDecision = await complete_json(llm_service, prompt, AgentDecision)
    return decision


def make_planner_node(llm_service: LLMService, session_factory, budget: AgentBudget):
    async def plan(state) -> dict:
        run_id = state["run_id"]
        step = counter(state, "step_count") + 1
        allowed = allowed_actions(state, budget)

        # 硬性用尽后的强制 finish 必须绕开可行性过滤：那一步的意图恰恰是
        # "没有任何动作可行了，出去"，再过滤一次会把它换成别的动作，图就停不下来。
        forced = steps_hard_exhausted(state, budget)
        decision = await _decide(state, allowed, budget, llm_service)

        if not forced and decision.action not in allowed:
            # 模型选了一个此刻不可行的动作。换成哪个由 pick_fallback 按固定顺序决定，
            # **不重问模型**——重问只会在同一组约束下再得到一次同类回答。
            #
            # 换掉动作时一并丢掉 target/arguments：它们是**针对原动作**的
            # （target 是选题原文、arguments 是检索词），换个动作后含义就变了，
            # 留着会让下一个工具拿到一个看似合理、实则指错对象的参数。
            fallback = pick_fallback(state, budget)
            logger.warning(
                "agent decision rejected (run_id=%s, wanted=%s, fallback=%s)",
                run_id,
                decision.action.value,
                fallback.value,
            )
            decision = AgentDecision(
                action=fallback,
                reason=(
                    f"（原决策 {decision.action.value} 在当前状态下不可行，"
                    f"按回退顺序改为 {fallback.value}）{decision.reason}"
                ),
            )

        entry = DecisionTraceEntry(
            step=step,
            action=decision.action.value,
            reason=decision.reason,
            target=decision.target,
            result_summary="",
            timestamp=datetime.now(timezone.utc),
        )

        async with session_factory() as session:
            await create_decision(
                session,
                run_id=run_id,
                step=step,
                action=decision.action.value,
                reason=decision.reason,
                target=decision.target,
                input_summary=summarize_input(state, allowed, budget),
            )

        logger.info("agent step %d (run_id=%s): %s", step, run_id, decision.action.value)
        return {
            "decision": decision,
            "step_count": step,
            # 先记意图，结果由执行节点在同一步上覆盖（见 merge_trace 的说明）。
            "decision_trace": [entry],
            "last_result": "",
        }

    return plan
