"""revise_candidate：按 Critic 的意见改**一条**候选。

三条约束：

1. **原地替换**，candidate_id 不变、`revision` +1。改稿产生的新候选会让 Critic
   面对一个没评价过的陌生人，而它刚刚给出的意见正是针对这一条的——
   换 id 等于把意见丢了。
2. 改的必须是 Critic 指着的那条（`critic_result.candidate_id`），Planner 可以用
   `target` 覆盖，但覆盖不到候选时回退到 Critic 指的那条，不会去改别的一条。
3. 改完 `writer_epoch` 进位，于是 `criticize` 重新可行——改稿必须能被重新评价，
   否则"改完就算过"了，修订轮次也就失去了意义。
"""

import logging

from app.agent.budget import counter
from app.agent.persona import Persona
from app.agent.prompts import (
    format_critics,
    format_evidence,
    format_persona,
    load_prompt,
    render,
)
from app.agent.schemas import Candidate, CandidateDraft, CriticResult
from app.agent.tools.base import ToolResult, complete_json
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


def _pick_target(candidates: list[Candidate], critic: CriticResult, target: str | None) -> Candidate | None:
    """挑出要改的那条候选。"""
    wanted = target or critic.candidate_id
    picked = next((c for c in candidates if c.candidate_id == wanted), None)
    if picked is not None:
        return picked
    return next((c for c in candidates if c.candidate_id == critic.candidate_id), None)


def make_revise_tool(llm_service: LLMService, persona: Persona):
    async def revise(state, decision) -> ToolResult:
        run_id = state["run_id"]
        candidates: list[Candidate] = list(state.get("candidates") or [])
        critic = state.get("critic_result")
        if critic is None or not candidates:  # pragma: no cover - 前置条件已挡住
            return ToolResult(delta={}, summary="没有可改稿的候选")

        target = _pick_target(candidates, critic, decision.target)
        if target is None:  # pragma: no cover - 防御性分支
            return ToolResult(delta={}, summary="Critic 指向的候选不在池子里，未改稿")

        angle = state.get("selected_angle")
        logger.info(
            "agent revise started (run_id=%s, candidate=%s, revision=%d)",
            run_id,
            target.candidate_id,
            target.revision + 1,
        )

        prompt = render(
            load_prompt("revise"),
            GOAL=state.get("goal") or state.get("topic") or "",
            TOPIC=state.get("selected_topic") or "",
            CLAIM=getattr(angle, "claim", ""),
            EVIDENCE=format_evidence(state.get("evidence_pack")),
            PERSONA=format_persona(persona),
            MAX_LENGTH=persona.max_length,
            CANDIDATE=target.content,
            LABEL=target.label,
            REVISION_TARGETS="；".join(critic.revision_targets) or "（Critic 未给出具体修改目标）",
            FLAGS="；".join(critic.flags) or "（无）",
            PRIOR_CRITICS=format_critics(state.get("critic_results") or []),
        )
        draft: CandidateDraft = await complete_json(llm_service, prompt, CandidateDraft)

        updated = target.model_copy(
            update={
                "content": draft.content,
                "label": draft.label or target.label,
                "revision": target.revision + 1,
            }
        )
        pool = [updated if c.candidate_id == target.candidate_id else c for c in candidates]

        logger.info("agent revise completed (run_id=%s, candidate=%s)", run_id, target.candidate_id)
        return ToolResult(
            delta={
                "candidates": pool,
                "revision_count": counter(state, "revision_count") + 1,
                "writer_epoch": counter(state, "writer_epoch") + 1,
            },
            summary=(
                f"改写 {target.candidate_id} → 第 {updated.revision} 稿，"
                f"针对：{'；'.join(critic.revision_targets) or '（未给出具体目标）'}"
            ),
        )

    return revise
