"""generate_candidates：按选定观点写候选正文。

工作单第十一节的两条要求在这里落地：

1. **输入完整**：Goal + EvidencePack + SelectedAngle + Persona + 中文写作规则。
   缺任何一项都会让 Writer 去补它自己没有的信息——少了证据包它就开始编事实，
   少了 Persona 它就写成通用 AI 腔。
2. **默认不止一条**。三条候选在**写法**上必须不同（直接观点 / 信息密集 / 反共识），
   否则"生成三条"只是同一句话换三种措辞，Critic 面对三条同质候选也无从比较。

候选池是累积的（`max_candidates` 约束的是全池容量，见 app/agent/budget.py 的说明），
所以这里要按剩余容量截断——不截断的话多轮生成会让池子超过预算，
而预算存在的意义正是"模型觉得还该再写一条时仍然能停下来"。
"""

import logging

from app.agent.budget import AgentBudget, counter
from app.agent.persona import Persona
from app.agent.prompts import (
    format_evidence,
    format_persona,
    load_prompt,
    render,
)
from app.agent.schemas import Candidate, CandidateList
from app.agent.tools.base import ToolResult, complete_json
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


def make_candidates_tool(llm_service: LLMService, persona: Persona, budget: AgentBudget):
    async def generate_candidates(state, decision) -> ToolResult:
        run_id = state["run_id"]
        existing: list[Candidate] = list(state.get("candidates") or [])
        capacity = budget.max_candidates - len(existing)
        if capacity <= 0:  # pragma: no cover - 前置条件已挡住，防御性分支
            return ToolResult(delta={}, summary="候选池已满，未生成新候选")

        wanted = min(budget.candidates_per_round, capacity)
        angle = state.get("selected_angle")
        logger.info(
            "agent candidates started (run_id=%s, angle=%s, want=%d)",
            run_id,
            getattr(angle, "angle_id", None),
            wanted,
        )

        prompt = render(
            load_prompt("candidates"),
            GOAL=state.get("goal") or state.get("topic") or "",
            TOPIC=state.get("selected_topic") or "",
            EVIDENCE=format_evidence(state.get("evidence_pack")),
            CLAIM=getattr(angle, "claim", ""),
            WHY_IT_MATTERS=getattr(angle, "why_it_matters", ""),
            PERSONA=format_persona(persona),
            MAX_LENGTH=persona.max_length,
            COUNT=wanted,
        )
        parsed: CandidateList = await complete_json(llm_service, prompt, CandidateList)

        fresh: list[Candidate] = []
        for candidate in parsed.candidates[:wanted]:
            # candidate_id 由这里赋，不采信模型给的值：Critic 与 revise 都按 id
            # 定位候选，重号或跳号会让它们改到别的一条上。
            fresh.append(
                candidate.model_copy(
                    update={
                        "candidate_id": f"c{len(existing) + len(fresh) + 1}",
                        "revision": 0,
                    }
                )
            )

        if not fresh:
            # 交给上层继续决策：没有新候选意味着 writer_epoch 不变，
            # criticize 的前置条件也就不会满足（不会去评价一批没写过的东西）。
            return ToolResult(delta={}, summary="模型没有产出可用候选")

        logger.info("agent candidates completed (run_id=%s, new=%d)", run_id, len(fresh))
        labels = "；".join(f"{c.candidate_id}（{c.label}）" for c in fresh)
        return ToolResult(
            delta={
                "candidates": existing + fresh,
                # writer_epoch 表示"候选池被改动过几次"。criticize 靠它判断
                # 自己评的是不是最新的那批（见 budget.allowed_actions）。
                "writer_epoch": counter(state, "writer_epoch") + 1,
            },
            summary=f"写出 {len(fresh)} 条候选：{labels}",
        )

    return generate_candidates
