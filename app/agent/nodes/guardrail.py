"""Guardrail 节点：确定性规则检查，不调用 LLM。

V1 只做规则判定，结论作为信息交给人工审核者（不阻断 Graph，见 Phase 3 决策）。
规则本体是纯函数 `evaluate()`，可脱离数据库与 Graph 单独单元测试。
"""

import logging
from typing import Iterable

from app.agent.persona import Persona
from app.agent.state import RunState
from app.agent.viewpoint import evidence_index
from app.repositories import list_published_contents
from app.style.gate import evaluate_content_gate

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
    fact_ids: Iterable[str] | None = None,
) -> tuple[bool, list[str]]:
    """对一条正文执行全部规则，返回 (是否通过, 原因码列表)。

    `fact_ids` 是本轮证据的事实编号，交给内容门禁判断裸形编号（`f18`）算不算
    泄漏。取不到证据时传 `None`——那时门禁只启用零假阳性的括号形。
    """
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

    # 内容门禁（内部编号泄漏 / 低质点击诱饵）。**只在违规时**产出原因码：
    # 既有测试断言的是一个精确列表，干净正文必须一个码都不多。
    #
    # 这里保持"信息性、不阻断 Graph"的契约——命中只是给人工审核者多一条信息。
    # 真正把它拦下来的是 `PublishService._preflight`，调的是同一个函数。
    for violation in evaluate_content_gate(content, fact_ids=fact_ids):
        reasons.append(violation.code)

    return (not reasons), reasons


def make_guardrail_node(persona: Persona, session_factory):
    async def guardrail_node(state: RunState) -> dict:
        run_id = state["run_id"]
        content = state.get("draft_content")
        logger.info("guardrail started (run_id=%s)", run_id)

        async with session_factory() as session:
            published = await list_published_contents(session)

        # 本轮证据的事实编号。用 `evidence_index` 而不是自己从 state 里掏——
        # 它是全仓唯一一处"从证据包里取编号"的实现，抄一份迟早会与它漂开。
        fact_ids = evidence_index(state.get("evidence_pack")).fact_ids

        passed, reasons = evaluate(
            content,
            max_length=persona.max_length,
            published_contents=published,
            fact_ids=fact_ids,
        )
        logger.info(
            "guardrail completed (run_id=%s, passed=%s, reasons=%s)",
            run_id,
            passed,
            reasons,
        )
        return {"guardrail_passed": passed, "guardrail_reasons": reasons}

    return guardrail_node
