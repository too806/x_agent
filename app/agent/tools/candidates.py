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
    format_style_recipe,
    load_prompt,
    render,
)
from app.agent.schemas import Candidate, CandidateList
from app.agent.tools.base import ToolResult, complete_json
from app.services.llm import LLMService
from app.style.runtime import resolve_recipe

logger = logging.getLogger(__name__)


def make_candidates_tool(
    llm_service: LLMService,
    persona: Persona,
    budget: AgentBudget,
    session_factory=None,
):
    """构造 generate_candidates 工具。

    `session_factory` 是 Phase 5B.2 新加的：Writer 要拿到 StyleRecipe，而
    recipe 由存库的 Style Corpus Profile 推导出来，所以这里需要一次只读查询。
    discover / research 工具早就是这个路子，参数形状与它们一致。

    默认 `None` 是**刻意留的**：老测试直接构造工具时不传它，此时退化成一个
    不施加结构约束的中性 recipe（见 `app/style/runtime.py`），
    那些测试的行为与 5B.2 之前逐字相同。
    """

    async def generate_candidates(state, decision) -> ToolResult:
        run_id = state["run_id"]
        existing: list[Candidate] = list(state.get("candidates") or [])

        # 先备好 recipe 再判容量：风格决策要在**每条返回路径**上都落进 state，
        # 否则 Critic 那一轮可能拿不到 recipe 可判，而降级是静默的。
        # 已经选过就复用——整个运行里结构必须一致，否则两批候选的骨架不同，
        # Critic 也就没有统一的目标可比对。
        recipe = await resolve_recipe(
            session_factory,
            state.get("evidence_pack"),
            existing=state.get("style_recipe"),
            # 平台硬上限由这里递进去：风格层不 import persona（见 runtime 的说明），
            # 所以"最多能发多少字"必须从外面告诉它。guardrail 与发布路径用的
            # 是同一个 `persona.max_length`，三处同源。
            hard_max_length=persona.max_length,
        )
        style_delta = {"style_recipe": recipe}

        capacity = budget.max_candidates - len(existing)
        if capacity <= 0:  # pragma: no cover - 前置条件已挡住，防御性分支
            return ToolResult(delta=style_delta, summary="候选池已满，未生成新候选")

        wanted = min(budget.candidates_per_round, capacity)
        angle = state.get("selected_angle")
        logger.info(
            "agent candidates started (run_id=%s, angle=%s, want=%d, structure=%s)",
            run_id,
            getattr(angle, "angle_id", None),
            wanted,
            getattr(recipe.structure_type, "value", recipe.structure_type),
        )

        prompt = render(
            load_prompt("candidates"),
            GOAL=state.get("goal") or state.get("topic") or "",
            TOPIC=state.get("selected_topic") or "",
            EVIDENCE=format_evidence(state.get("evidence_pack")),
            CLAIM=getattr(angle, "claim", ""),
            WHY_IT_MATTERS=getattr(angle, "why_it_matters", ""),
            PERSONA=format_persona(persona),
            # STYLE **必须**每次都传：render 只替换传进去的键，不会因为缺键报错，
            # 漏传会让字面量 {{STYLE}} 原样进入发给模型的 prompt。
            STYLE=format_style_recipe(recipe),
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
            # style_recipe 仍然要写：这一轮虽然没产出候选，但"本次的结构"已经定了，
            # 而 Critic 下一轮读的正是 state 里这个字段。
            return ToolResult(delta=style_delta, summary="模型没有产出可用候选")

        logger.info("agent candidates completed (run_id=%s, new=%d)", run_id, len(fresh))
        labels = "；".join(f"{c.candidate_id}（{c.label}）" for c in fresh)
        return ToolResult(
            delta={
                "candidates": existing + fresh,
                # writer_epoch 表示"候选池被改动过几次"。criticize 靠它判断
                # 自己评的是不是最新的那批（见 budget.allowed_actions）。
                "writer_epoch": counter(state, "writer_epoch") + 1,
                **style_delta,
            },
            summary=f"写出 {len(fresh)} 条候选：{labels}",
        )

    return generate_candidates
