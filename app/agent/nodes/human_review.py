"""Human Review 节点：用 LangGraph 原生 interrupt() 暂停，等待人工决定。

关键约束（LangGraph 语义）：
- `interrupt()` 会抛 GraphInterrupt 暂停整个 Graph，并把 payload 交给调用方；
- resume 时本节点**从头重新执行**，`interrupt()` 这一次直接返回 resume value。
  因此 interrupt() 之前不能有任何不可重复的副作用（DB 写、外部调用）。
"""

import logging

from langgraph.types import interrupt

from app.agent.state import RunState
from app.repositories import create_review
from app.schemas import ReviewDecisionAdapter

logger = logging.getLogger(__name__)


def make_human_review_node(session_factory):
    async def human_review_node(state: RunState) -> dict:
        run_id = state["run_id"]
        draft_id = state.get("draft_id")
        content = state.get("draft_content")

        payload = {
            "type": "content_review",
            "run_id": run_id,
            "draft_id": draft_id,
            "content": content,
            "guardrail_passed": state.get("guardrail_passed", False),
            "guardrail_reasons": state.get("guardrail_reasons", []),
        }

        logger.info("review waiting (run_id=%s)", run_id)
        # interrupt 之前不写库：resume 时节点会重放
        raw_decision = interrupt(payload)

        # resume value 来自外部（HTTP 请求体），一律重新校验，不信任自由文本
        decision = ReviewDecisionAdapter.validate_python(raw_decision)
        logger.info("review resumed (run_id=%s, action=%s)", run_id, decision.action)

        edited_content = decision.content if decision.action == "edit" else None
        reject_reason = decision.reason if decision.action == "reject" else None

        async with session_factory() as session:
            await create_review(
                session,
                draft_id=draft_id,
                action=decision.action,
                original_content=content or "",
                edited_content=edited_content,
                reject_reason=reject_reason,
            )

        if decision.action == "approve":
            logger.info("review approved (run_id=%s)", run_id)
            return {"human_action": "approve", "final_content": content}

        if decision.action == "edit":
            logger.info("review edited (run_id=%s)", run_id)
            return {"human_action": "edit", "final_content": edited_content}

        logger.info("review rejected (run_id=%s)", run_id)
        # reject 不产生 final_content，流程在此结束（Phase 3 不发布）
        return {"human_action": "reject"}

    return human_review_node
