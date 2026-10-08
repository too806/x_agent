"""Human Review 节点：用 LangGraph 原生 interrupt() 暂停，等待人工决定。

关键约束（LangGraph 语义）：
- `interrupt()` 会抛 GraphInterrupt 暂停整个 Graph，并把 payload 交给调用方；
- resume 时本节点**从头重新执行**，`interrupt()` 这一次直接返回 resume value。
  因此 interrupt() 之前不能有任何不可重复的副作用（DB 写、外部调用）。
"""

import logging
from typing import Any, Mapping

from langgraph.types import interrupt

from app.agent.state import RunState
from app.models.review import DECIDED_BY_AUTO, DECIDED_BY_HUMAN
from app.repositories import create_review
from app.schemas import ReviewDecisionAdapter

logger = logging.getLogger(__name__)


def _decided_by_of(raw_decision: Any) -> str:
    """从**未经校验的** resume 值里取出"这个决定是谁做的"。

    必须在 `ReviewDecisionAdapter.validate_python` **之前**读，因为这个字段
    **刻意不在**决策 schema 里。`ReviewDecisionAdapter` 配的是 `extra="ignore"`，
    所以客户端在 HTTP 请求体里塞一个 `decided_by: "auto"` 会被 pydantic 直接丢掉——
    **HTTP 路径因此在结构上就无法声明自己是自动的**，不需要再写一段
    "把客户端的 auto 改回 human" 的补丁代码。补丁会被下一个人删掉，
    而"这个字段不在 schema 里"是一道删不掉的墙。

    自动侧绕过这道墙的方式是**直接给 `Command(resume=...)` 传原始 dict**，
    不经过 schema。这条路只有本进程内部的代码走得通。resume 值随 checkpoint
    持久化，所以进程崩了重放也保得住这个标记。

    无法识别的取值一律**按人处理**。方向是刻意的：错标成 auto 的后果是
    某条人工 Run 被"清理自动残留"的逻辑顺手收走（见
    `repositories.reap_stale_review_claims`），错标成 human 的后果只是
    影子一致率的统计略微失真。前者是事故，后者是噪音。
    """
    if isinstance(raw_decision, Mapping):
        value = raw_decision.get("decided_by")
        if value == DECIDED_BY_AUTO:
            return DECIDED_BY_AUTO
    return DECIDED_BY_HUMAN


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

        # 先读"谁做的决定"，再校验决定内容本身——顺序不能反，理由见 _decided_by_of
        decided_by = _decided_by_of(raw_decision)
        # resume value 来自外部（HTTP 请求体），一律重新校验，不信任自由文本
        decision = ReviewDecisionAdapter.validate_python(raw_decision)
        logger.info(
            "review resumed (run_id=%s, action=%s, decided_by=%s)",
            run_id,
            decision.action,
            decided_by,
        )

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
                decided_by=decided_by,
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
