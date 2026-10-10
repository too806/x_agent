"""revise_candidate：按 Critic 的意见改**一条**候选。

四条约束：

1. **原地替换**，candidate_id 不变、`revision` +1。改稿产生的新候选会让 Critic
   面对一个没评价过的陌生人，而它刚刚给出的意见正是针对这一条的——
   换 id 等于把意见丢了。
2. 改的必须是 Critic 指着的那条（`critic_result.candidate_id`），Planner 可以用
   `target` 覆盖，但覆盖不到候选时回退到 Critic 指的那条，不会去改别的一条。
3. 改完 `writer_epoch` 进位，于是 `criticize` 重新可行——改稿必须能被重新评价，
   否则"改完就算过"了，修订轮次也就失去了意义。
4. **改稿同时作废这一条的旧评价**（见函数末尾 delta 的说明）。

第 3 条与第 4 条管的不是同一件事，缺一不可：`writer_epoch` 只筑起"改过就还得再评
一次"这道**门槛**，它管不住已经躺在 `critic_results` 里的那份旧评分。而候选是
原地替换的、candidate_id 不变，`finalize` 又正是按 id 取分数——于是旧分数会
原封不动地挂到新正文头上。门槛只能拦住正常收尾，拦不住 `steps_hard_exhausted`
那条强制 finish 的路径（它刻意绕开 allowed_actions，见 nodes/planner.py）。
"""

import logging

from app.agent.budget import counter
from app.agent.persona import Persona
from app.agent.prompts import (
    format_context,
    format_critics,
    format_evidence,
    format_length_headroom,
    format_persona,
    format_style_recipe,
    format_viewpoint,
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
            # 与 Writer / Critic 同一份渲染：改稿也要知道"不能改掉的是哪个判断"。
            VIEWPOINT=format_viewpoint(angle),
            # 改稿同样是写作，同样要面对"读者不知道这个话题"。用同一份背景、
            # 同一句用途说明（`for_reader=True`），否则 Critic 判了"读者进不来"、
            # 改稿这一步却看不到该往哪补。
            CONTEXT=format_context(
                getattr(state.get("evidence_pack"), "context", None)
            ),
            EVIDENCE=format_evidence(state.get("evidence_pack")),
            PERSONA=format_persona(persona),
            # 改稿要收敛到同一个结构，否则改着改着骨架就换了。
            # 从 state 读，不重算：见 tools/__init__.py 的说明。
            STYLE=format_style_recipe(state.get("style_recipe")),
            MAX_LENGTH=persona.max_length,
            # 把"现在多少字、要砍多少"算好递给模型：它数不准中文字数，
            # 但"把 188 砍掉 48"是一个它做得到的任务。
            LENGTH_HEADROOM=format_length_headroom(target.content, persona.max_length),
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

        # 改稿让"针对这一条的旧评价"作废。两件事必须一起做：
        #
        # 1. 从 critic_results 里摘掉这一条的旧评价。finalize 按 candidate_id 打分，
        #    而改稿**保留 candidate_id**——留着旧评价，就是用改稿**前**的分数给
        #    改稿**后**的正文背书。摘掉之后它落回 finalize 的"未被评价"一档，
        #    既不会拿到旧分数，也不会被当成已评价过的候选。
        # 2. 清空 critic_result。`revision_pending` 只看这一个字段，不清空的话
        #    revise 仍然可行、且排在 FALLBACK_ORDER 首位，于是可以连着改两次——
        #    而第二次拿到的 revision_targets 是针对**上一版**文本写的，
        #    等于照着旧意见改新稿，改稿循环因此空转。
        #
        # 改完 writer_epoch 已经进位，budget 那边照样要求重新评价
        # （ready_to_finish 要 writer_epoch <= critic_epoch），那一层不用动。
        stale_id = target.candidate_id
        remaining = [
            r for r in (state.get("critic_results") or []) if r.candidate_id != stale_id
        ]

        logger.info("agent revise completed (run_id=%s, candidate=%s)", run_id, stale_id)
        return ToolResult(
            delta={
                "candidates": pool,
                "critic_results": remaining,
                "critic_result": None,
                "revision_count": counter(state, "revision_count") + 1,
                "writer_epoch": counter(state, "writer_epoch") + 1,
            },
            summary=(
                f"改写 {stale_id} → 第 {updated.revision} 稿，旧评价已作废、需重新评价；"
                f"针对：{'；'.join(critic.revision_targets) or '（未给出具体目标）'}"
            ),
        )

    return revise
