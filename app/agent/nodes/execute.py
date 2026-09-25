"""执行节点：把 Planner 的决策交给对应的工具，并把结果记进轨迹。

这里有一个容易忽略的细节：节点是**每个动作一个**（由 `make_tool_node` 生成），
而不是一个大 dispatch 节点。两者行为等价，但分开之后 LangGraph 的 checkpoint 里
每一步是哪个节点、`snapshot.next` 是什么，都能直接读出来——调试一条
"它为什么先研究了三轮"的运行时，这比读一个 dispatch 函数快得多。

Planner 与执行分离还有一条实际作用：**Planner 不能自己执行**（第五节）。
它拿到的是一个动作名，实际动数据库、调 X、调 LLM 的都是这里，于是
"Planner 不写 SQL、不碰 httpx"（第十八节）不是靠自觉，而是结构上做不到。
"""

import logging
from datetime import datetime, timezone

from app.agent.budget import counter
from app.agent.schemas import AgentAction, DecisionTraceEntry
from app.repositories import record_decision_result

logger = logging.getLogger(__name__)


def make_tool_node(action: AgentAction, tools, session_factory):
    tool = tools[action]

    async def execute(state) -> dict:
        run_id = state["run_id"]
        decision = state["decision"]
        step = counter(state, "step_count")

        logger.info("agent step %d executing %s (run_id=%s)", step, action.value, run_id)
        result = await tool(state, decision)

        # 把 Planner 写下的那条（只有意图）补上结果，用同一个 step 覆盖。
        prior = next(
            (e for e in (state.get("decision_trace") or []) if e.step == step), None
        )
        entry = (
            prior
            if prior is not None
            else DecisionTraceEntry(
                step=step,
                action=action.value,
                reason=decision.reason,
                target=decision.target,
                timestamp=datetime.now(timezone.utc),
            )
        ).model_copy(update={"result_summary": result.summary})

        async with session_factory() as session:
            await record_decision_result(
                session, run_id=run_id, step=step, result_summary=result.summary
            )

        logger.info("agent step %d done (run_id=%s): %s", step, run_id, result.summary)
        return {**result.delta, "decision_trace": [entry], "last_result": result.summary}

    return execute
