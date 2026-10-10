"""generate_angles / select_angle：形成并选定观点。

工作单第十节第一版的目标很克制，就一句话：
**"Agent 会不会自己形成一个明确、可证据支持的观点"**。

Phase 5B.3 把这条判断补上边界：一个观点不再只是 `claim` 一句话，而是
**判断 + 推理 + 依据 + 反方 + 适用范围**（工作单第五、六节）。但**类名与动作名
不变**，state 键也不变（`angles` / `selected_angle`）——工作单第四节要求
"最终定义以现有代码为准"，而审计结论是 `Angle.claim` 今天就已经是判断、
不是方向，所以缺的不是第二个模型，是这条判断的依据与边界。

三条约束照旧，一条新增：

1. 每个 Angle 的 evidence_ids 必须落在证据包真实的 fact_id 集合里，
   引用不到的论点**直接丢弃**——与 evidence 工具的处置一致。
   没有证据支撑的论点不是"差一点的论点"，它是一个会被模型用漂亮话讲出来的断言。
2. Angle 只能引 fact_id，不能引原始材料（见 app/agent/schemas.py 的说明）。
3. 反方同理：声明了却引不到真实 fact 的反方**不计分**（工作单第八节）。
4. **新增**：分数全部由 `app/agent/viewpoint.py` 从真实观测算出，模型的输出里
   没有任何分数字段（`AngleDraft`）。`confidence` 不再采信模型给的值
   （工作单第十一节）。
"""

import logging

from app.agent.prompts import format_context, format_evidence, load_prompt, render
from app.agent import viewpoint
from app.agent.schemas import Angle, AngleDraftList, ViewpointAssessment
from app.agent.tools.base import ToolResult, complete_json
from app.services.llm import LLMService

logger = logging.getLogger(__name__)

MAX_ANGLES = 5
MIN_ANGLES = 3


def _assessment_of(score: viewpoint.ViewpointScore) -> ViewpointAssessment:
    """把打分结果摊进 Angle 上的评估明细。"""
    return ViewpointAssessment(
        evidence_support=score.evidence_support,
        evidence_support_raw=score.evidence_support_raw,
        source_diversity=score.source_diversity,
        counterpoint_awareness=score.counterpoint_awareness,
        unknown_pressure=score.unknown_pressure,
        specificity=score.specificity,
        novelty=score.novelty,
        newness_contribution=score.newness_contribution,
        confidence_raw=score.confidence_raw,
        viewpoint_score_raw=score.viewpoint_score_raw,
        bound_facts=score.bound_facts,
        total_facts=score.total_facts,
        bound_sources=score.bound_sources,
        bound_authors=score.bound_authors,
        missing_author_sources=score.missing_author_sources,
        declared_counterpoints=score.declared_counterpoints,
        bound_counterpoints=score.bound_counterpoints,
        total_materials=score.total_materials,
        unreferenced_materials=score.unreferenced_materials,
        breadth_markers=list(score.breadth_markers),
        motive_markers=list(score.motive_markers),
        motive_unsupported=score.motive_unsupported,
        originality_claim=score.originality_claim,
        restates_fact=score.restates_fact,
        duplicate_group=score.duplicate_group,
        reasons=list(score.reasons),
    )


def make_angles_tool(llm_service: LLMService):
    async def generate_angles(state, decision) -> ToolResult:
        run_id = state["run_id"]
        pack = state.get("evidence_pack")
        fact_ids = {fact.fact_id for fact in getattr(pack, "facts", [])}
        logger.info("agent angles started (run_id=%s, facts=%d)", run_id, len(fact_ids))

        prompt = render(
            load_prompt("angles"),
            GOAL=state.get("goal") or state.get("topic") or "",
            TOPIC=state.get("selected_topic") or "",
            MIN_ANGLES=MIN_ANGLES,
            MAX_ANGLES=MAX_ANGLES,
            EVIDENCE=format_evidence(pack),
            # 观点环节拿到的是**同一段背景**，但用另一句用途说明（`for_reader=False`）：
            # 这里要防的是"从背景推出一个结论"，而那是另一件事——见工作单第七节。
            # 同一份内容 + 两套说明，比抄两份文本安全：抄两份必然漂，漂了不报错。
            CONTEXT=format_context(getattr(pack, "context", None), for_reader=False),
        )
        parsed: AngleDraftList = await complete_json(
            llm_service, prompt, AngleDraftList
        )

        # 先丢掉引不到证据的草稿，再打分：让一个没有依据的草稿参与排序，
        # 只是给"说得漂亮"一次拿高分的机会。
        kept: list = []
        dropped = 0
        dropped_counterpoints = 0
        for draft in parsed.angles[:MAX_ANGLES]:
            valid = [eid for eid in draft.evidence_ids if eid in fact_ids]
            if not valid:
                dropped += 1
                continue
            # 反方引不到真实事实就丢掉引用，**但保留那句话**——观点仍然"提到过"
            # 这个反方，只是它不可核验，因此拿不到绑定分（见 viewpoint.py）。
            valid_counterpoints = [
                eid for eid in draft.counterpoint_evidence_ids if eid in fact_ids
            ]
            dropped_counterpoints += len(draft.counterpoint_evidence_ids) - len(
                valid_counterpoints
            )
            kept.append(
                draft.model_copy(
                    update={
                        "evidence_ids": valid,
                        "counterpoint_evidence_ids": valid_counterpoints,
                    }
                )
            )

        index = viewpoint.evidence_index(
            pack,
            state.get("research_item_ids") or [],
            state.get("research_results") or [],
        )
        scores = viewpoint.score_viewpoints(kept, index, total_facts=len(fact_ids))

        # 编号在**排序之后**赋，理由照 tools/discover.py 已经写下的那条：
        # format_state 里 id 与分数会同时出现，一个名次对不上分数的编号
        # （a3 比 a1 分高）比没有编号更误导。
        scored = sorted(zip(scores, kept), key=lambda pair: viewpoint.rank_key(pair[0]))
        angles: list[Angle] = []
        for position, (score, draft) in enumerate(scored, start=1):
            angles.append(
                Angle(
                    angle_id=f"a{position}",
                    claim=draft.claim,
                    why_it_matters=draft.why_it_matters,
                    evidence_ids=list(draft.evidence_ids),
                    risk=draft.risk,
                    confidence=score.confidence,
                    reasoning=draft.reasoning,
                    counterpoints=list(draft.counterpoints),
                    counterpoint_evidence_ids=list(draft.counterpoint_evidence_ids),
                    assumptions=list(draft.assumptions),
                    implications=list(draft.implications),
                    novelty_note=draft.novelty_note,
                    restates_known_material=draft.restates_known_material,
                    stance=score.stance,
                    stance_declared=score.stance_declared,
                    max_stance=score.max_stance,
                    claim_scope=score.claim_scope,
                    evidence_scope=score.evidence_scope,
                    scope_exceeds_evidence=score.scope_exceeds_evidence,
                    scope_declared_missing=score.scope_declared_missing,
                    viewpoint_score=score.viewpoint_score,
                    assessment=_assessment_of(score),
                )
            )

        logger.info("agent angles completed (run_id=%s, angles=%d)", run_id, len(angles))
        summary = "生成 %d 个可证据支撑的观点：" % len(angles) + "；".join(
            f"{a.angle_id}（{a.viewpoint_score:.0f} 分，{a.stance.value}）{a.claim}"
            for a in angles
        )
        if dropped:
            summary += f"（丢弃 {dropped} 个引不到证据的）"
        if dropped_counterpoints:
            summary += f"（{dropped_counterpoints} 条反方引用核验不上，按未绑定计）"
        # 重新生成意味着旧的选定已经不属于这一批：不清掉的话 `selected_angle`
        # 会继续指向一个不在 `angles` 里的对象，而 Writer 照着它写稿——
        # 没有报错，只是这一轮的观点与正文对不上。Phase 5A 起这条路径不可达
        # （budget 在有候选之后挡掉了 generate_angles），但研究回路把它变成可达。
        return ToolResult(delta={"angles": angles, "selected_angle": None}, summary=summary)

    return generate_angles


def make_select_angle_tool():
    """选定观点。

    这一步**不调用 LLM**，与 `select_topic` 同一套做法：分数已经由
    `app/agent/viewpoint.py` 算好并排好序，让模型再"选择"一次只会给它一次推翻
    确定性排序的机会，而 trace 里记下的"为什么选它"就变成了模型的自述而不是
    分数（工作单第二十节）。

    target 认 `angle_id`（a1、a2…）或 claim 原文；两种都不是、或者给的那个
    比最高分低出容差（`viewpoint.TIE_MARGIN`）时，回退到确定性排序的第一名，
    并把**差了多少分**写进 summary——工作单第二十一节要求这个判定是可解释的，
    而"回退到最高分"不带数字时，看不出是险胜还是碾压。
    """

    async def select_angle(state, decision) -> ToolResult:
        angles: list[Angle] = list(state.get("angles") or [])
        if not angles:  # pragma: no cover - 前置条件已挡住，防御性分支
            return ToolResult(delta={}, summary="没有可选的观点")

        # target 可能是 id 也可能是 claim。先在这里解析成 claim，
        # 让 `viewpoint.select_viewpoint` 保持对 id 零依赖（id 由工具赋，
        # 评分层不该知道它的存在）。
        wanted = (decision.target or "").strip()
        by_id = next((a for a in angles if a.angle_id == wanted), None)
        target_claim = by_id.claim if by_id is not None else wanted

        pack = state.get("evidence_pack")
        index = viewpoint.evidence_index(
            pack,
            state.get("research_item_ids") or [],
            state.get("research_results") or [],
        )
        total_facts = len({fact.fact_id for fact in getattr(pack, "facts", [])})
        # 从 Angle 反推分数：老 checkpoint 里的 Angle 没有 assessment，
        # 这时**重算**而不是相信存下来的值——存下来的值只是给报告看的，
        # 排序的依据必须是当下这份输入算出来的。
        scores = viewpoint.score_viewpoints(angles, index, total_facts=total_facts)
        selection = viewpoint.select_viewpoint(scores, target=target_claim)
        if selection is None:  # pragma: no cover - 上面已挡住空列表
            return ToolResult(delta={}, summary="没有可选的观点")

        chosen = angles[selection.index]
        # 反推出来的分数可能与 Angle 上存的不一致（例如换过口径的历史数据）。
        # 以重算的为准写回，免得 state 里留着一份与排序依据不同的数。
        chosen = chosen.model_copy(
            update={
                "confidence": scores[selection.index].confidence,
                "viewpoint_score": scores[selection.index].viewpoint_score,
                "stance": scores[selection.index].stance,
            }
        )

        logger.info(
            "agent angle selected (run_id=%s, angle=%s, ambiguous=%s)",
            state["run_id"],
            chosen.angle_id,
            selection.ambiguous,
        )
        summary = f"{selection.how} → {chosen.angle_id}：{chosen.claim}"
        return ToolResult(delta={"selected_angle": chosen}, summary=summary)

    return select_angle
