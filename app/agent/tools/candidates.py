"""generate_candidates：按选定观点写候选正文。

工作单第十一节的两条要求在这里落地：

1. **输入完整**：Goal + EvidencePack + SelectedAngle + Persona + 中文写作规则。
   缺任何一项都会让 Writer 去补它自己没有的信息——少了证据包它就开始编事实，
   少了 Persona 它就写成通用 AI 腔。
2. **默认不止一条**。三条候选在**写法**上必须不同（鲜明观点型 / 事实与反差型 /
   讨论与不同视角型，定义在 `app/agent/post_structure.py`），
   否则"生成三条"只是同一句话换三种措辞，Critic 面对三条同质候选也无从比较。

候选池是累积的（`max_candidates` 约束的是全池容量，见 app/agent/budget.py 的说明），
所以这里要按剩余容量截断——不截断的话多轮生成会让池子超过预算，
而预算存在的意义正是"模型觉得还该再写一条时仍然能停下来"。
"""

import logging

from app.agent.budget import AgentBudget, counter
from app.agent.persona import Persona
from app.agent.post_structure import (
    evidence_author_count,
    evidence_shape,
    render_candidate_roles,
    render_ending_moves,
    render_evidence_shape,
    render_opening_moves,
)
from app.agent.prompts import (
    format_context,
    format_evidence,
    format_feedback_context,
    format_persona,
    format_style_recipe,
    format_viewpoint,
    load_prompt,
    render,
)
from app.agent.schemas import Candidate, CandidateList
from app.agent.tools.base import ToolResult, complete_json
from app.feedback.runtime import resolve_feedback_context
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
        # Phase 5B.4：反馈上下文与 recipe 并排备好。两者都是"这次运行拿到了什么
        # 输入"的运行期决策，都要在**每条返回路径**上落进 state——漏掉任何一条，
        # 那一次运行就会在最需要解释的时候缺失"当时看到了什么"。
        feedback = await resolve_feedback_context(
            session_factory,
            existing=state.get("feedback_context"),
        )
        # 两个 delta 合成一个：返回路径有三条，逐个列举很容易在某一条上漏掉一个。
        context_delta = {"style_recipe": recipe, "feedback_context": feedback}

        capacity = budget.max_candidates - len(existing)
        if capacity <= 0:  # pragma: no cover - 前置条件已挡住，防御性分支
            return ToolResult(delta=context_delta, summary="候选池已满，未生成新候选")

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
            # CONTEXT **必须**每次都传，理由同 STYLE / VIEWPOINT：render 不会因为
            # 缺键报错，漏传会让字面量 `{{CONTEXT}}` 原样进入发给模型的 prompt。
            # 无背景时它返回的是一段交代清楚的说明文字，不是空串。
            CONTEXT=format_context(
                getattr(state.get("evidence_pack"), "context", None)
            ),
            EVIDENCE=format_evidence(state.get("evidence_pack")),
            # 证据的**形状**（有没有可核验的数字、事实来自几条材料、几位作者）由
            # 程序统计后写死成指令。它必须由这里算：让模型自己判断"我手上有没有
            # 数字"，它在没有数字的时候照样会写出一个「约 30%」。
            EVIDENCE_SHAPE=render_evidence_shape(
                evidence_shape(
                    state.get("evidence_pack"),
                    author_count=evidence_author_count(state),
                )
            ),
            # 三条候选的三条路 + 开头/结尾形式的清单，全部来自
            # `app/agent/post_structure.py`：prompt、Critic、测试读同一份定义，
            # 三处各写一遍必然漂，而漂了不会报错。
            CANDIDATE_ROLES=render_candidate_roles(),
            OPENING_MOVES=render_opening_moves(),
            ENDING_MOVES=render_ending_moves(),
            # VIEWPOINT **必须**每次都传，理由同 STYLE：render 不会因为缺键报错，
            # 漏传会让字面量 {{VIEWPOINT}} 原样进入 prompt。
            # 用同一个渲染函数也是刻意的——Critic 那边读的是同一份文本，
            # 两边看到的目标不同时，Critic 会拿另一个目标去判"有没有写对"。
            VIEWPOINT=format_viewpoint(angle),
            PERSONA=format_persona(persona),
            # STYLE **必须**每次都传：render 只替换传进去的键，不会因为缺键报错，
            # 漏传会让字面量 {{STYLE}} 原样进入发给模型的 prompt。
            STYLE=format_style_recipe(recipe),
            # FEEDBACK **必须**每次都传，理由同 STYLE / VIEWPOINT：漏传会让
            # 字面量 `{{FEEDBACK}}` 原样进入 prompt。这里给的是**过去结果的观察**，
            # 不是写作要求（工作单第四十二节）。
            FEEDBACK=format_feedback_context(feedback),
            # 长度**目标区间**的下沿。上沿 `MAX_LENGTH` 同时是 guardrail 与
            # 发布路径执行的那条线（三处同源），下沿只是写作偏好——见
            # `Persona.recommended_min_length` 的说明。
            MIN_LENGTH=persona.recommended_min_length or 0,
            MAX_LENGTH=persona.max_length,
            COUNT=wanted,
        )
        parsed: CandidateList = await complete_json(llm_service, prompt, CandidateList)

        # viewpoint_ref 由这里赋，不采信模型给的值（同 candidate_id 的理由）：
        # 它是"这条正文是照哪个观点写的"的唯一凭证，Critic 靠它判断
        # "正文是否忠实于它该表达的观点"（工作单第二十六节）。让模型自己填，
        # 等于让被检查的一方填写检查依据。
        viewpoint_ref = getattr(angle, "angle_id", "") or ""

        fresh: list[Candidate] = []
        for candidate in parsed.candidates[:wanted]:
            # candidate_id 由这里赋，不采信模型给的值：Critic 与 revise 都按 id
            # 定位候选，重号或跳号会让它们改到别的一条上。
            fresh.append(
                candidate.model_copy(
                    update={
                        "candidate_id": f"c{len(existing) + len(fresh) + 1}",
                        "revision": 0,
                        "viewpoint_ref": viewpoint_ref,
                    }
                )
            )

        if not fresh:
            # 交给上层继续决策：没有新候选意味着 writer_epoch 不变，
            # criticize 的前置条件也就不会满足（不会去评价一批没写过的东西）。
            # style_recipe 仍然要写：这一轮虽然没产出候选，但"本次的结构"已经定了，
            # 而 Critic 下一轮读的正是 state 里这个字段。
            return ToolResult(delta=context_delta, summary="模型没有产出可用候选")

        logger.info("agent candidates completed (run_id=%s, new=%d)", run_id, len(fresh))
        labels = "；".join(f"{c.candidate_id}（{c.label}）" for c in fresh)
        return ToolResult(
            delta={
                "candidates": existing + fresh,
                # writer_epoch 表示"候选池被改动过几次"。criticize 靠它判断
                # 自己评的是不是最新的那批（见 budget.allowed_actions）。
                "writer_epoch": counter(state, "writer_epoch") + 1,
                **context_delta,
            },
            summary=f"写出 {len(fresh)} 条候选：{labels}",
        )

    return generate_candidates
