"""Guardrail 节点：确定性规则检查，不调用 LLM。

V1 只做规则判定，结论作为信息交给人工审核者（不阻断 Graph，见 Phase 3 决策）。
规则本体是纯函数 `evaluate()`，可脱离数据库与 Graph 单独单元测试。
"""

import logging

from app.agent.persona import Persona
from app.agent.state import RunState
from app.repositories import list_published_contents

logger = logging.getLogger(__name__)

# V1 基础违禁内容表：小写子串匹配，只覆盖明确不可发布的类别（垃圾推广/诱导类）。
# 这是种子表，后续可按账号策略替换为正式规则源。
FORBIDDEN_TERMS: tuple[str, ...] = (
    "buy followers",
    "free crypto giveaway",
    "click here to win",
)


def _has_forbidden_term(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in FORBIDDEN_TERMS)


def _is_well_formed(text: str) -> bool:
    """基础格式检查：不含控制字符，且至少有一个字母或数字。"""
    if any(not ch.isprintable() and ch not in "\n\t" for ch in text):
        return False
    return any(ch.isalnum() for ch in text)


def evaluate(
    content: str | None,
    *,
    max_length: int,
    published_contents: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """对一条正文执行全部规则，返回 (是否通过, 原因码列表)。"""
    text = (content or "").strip()
    if not text:
        # 空内容下其余规则没有意义，直接返回
        return False, ["empty_content"]

    reasons: list[str] = []
    if len(content or "") > max_length:
        reasons.append("too_long")

    published = {(item or "").strip() for item in (published_contents or [])}
    if text in published:
        reasons.append("duplicate_content")

    if not _is_well_formed(content or ""):
        reasons.append("invalid_format")

    if _has_forbidden_term(text):
        reasons.append("forbidden_content")

    return (not reasons), reasons


def make_guardrail_node(persona: Persona, session_factory):
    async def guardrail_node(state: RunState) -> dict:
        run_id = state["run_id"]
        content = state.get("draft_content")
        logger.info("guardrail started (run_id=%s)", run_id)

        async with session_factory() as session:
            published = await list_published_contents(session)

        passed, reasons = evaluate(
            content, max_length=persona.max_length, published_contents=published
        )
        logger.info(
            "guardrail completed (run_id=%s, passed=%s, reasons=%s)",
            run_id,
            passed,
            reasons,
        )
        return {"guardrail_passed": passed, "guardrail_reasons": reasons}

    return guardrail_node
