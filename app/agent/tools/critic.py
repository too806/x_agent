"""criticize_candidate：评价候选，**不改稿**（工作单第十二节）。

这条边界是 Critic 存在的全部意义。一个既能评价又能改稿的 Critic，实际上是一个
没有约束的 Writer：它会把"这地方说得不够狠"直接变成"我替你写狠一点"，
于是评价与写作互相掩盖，出了问题分不清是判断错了还是笔头错了。

所以这里只有一次 LLM 调用、产出结构化评价，正文一个字都不动。

评价是**整批一次**的（见 `CriticReport` 的说明）。三条候选的差别在写法上，
分开评价等于让 Critic 在自己看不见另外两条的情况下回答"哪条更适合这个角度"。
"""

import logging
from typing import Any

from app.agent import viewpoint
from app.agent.budget import counter
from app.agent.persona import Persona
from app.agent.post_structure import (
    evidence_author_count,
    evidence_shape,
    length_detail,
    over_hard_limit,
    render_candidate_roles,
    render_evidence_shape,
)
from app.agent.prompts import (
    format_candidates,
    format_context,
    format_context_signals,
    format_evidence,
    format_persona,
    format_scope_signals,
    format_style_recipe,
    format_viewpoint,
    load_prompt,
    render,
)
from app.agent.schemas import Candidate, CriticReport, CriticResult
from app.agent.tools.base import ToolResult, complete_json, sources
from app.services.llm import LLMService
from app.style.signals import ai_style_signals, format_signals

logger = logging.getLogger(__name__)


def format_ai_signals(candidates: list[Candidate]) -> str:
    """每条候选的确定性 AI 味信号，一行一条。

    每条都印（哪怕是"未发现"）：模型要知道"这一条**没有**那些痕迹"，
    才能把它与"这里没给数据"区分开。漏掉一条会让模型以为那条没被检查过。
    """
    if not candidates:
        return "（无候选）"
    return "\n".join(
        f"[{c.candidate_id}] {format_signals(ai_style_signals(c.content))}" for c in candidates
    )


def _bind_to_candidates(report: CriticReport, candidate_ids: list[str]) -> list[CriticResult]:
    """把评价结果对到真实候选上。

    先按 candidate_id 精确匹配。**全部匹配不上时**按顺序兜底：模型有相当的概率
    把 id 抄错（少一位、串行、写成 "candidate_1"），而一条抄错 id 但内容完好的
    评价，和一个"没有评价"之间差着整整一次 revise 循环。位置是这个 prompt 里
    唯一稳定的对应关系——候选是按顺序印进去的。

    只有**全部**匹配不上才走兜底：一半匹配一半不匹配时，位置兜底会把已经对上
    的那条覆盖成另一条的评价，那比丢掉一条评价更糟。

    匹配上之后还要**按 id 去重**，保留第一次出现的那条。模型很容易对同一条候选
    评两次（换个说法再来一遍），而重复项会让"评价了 N 条"这个数字虚高、
    让 `critic_result` 指向一条副本——两者的表现都是"看起来正常但数字对不上"。
    """
    known = set(candidate_ids)

    def dedupe(results: list[CriticResult]) -> list[CriticResult]:
        seen: set[str] = set()
        unique: list[CriticResult] = []
        for result in results:
            if result.candidate_id in seen:
                continue
            seen.add(result.candidate_id)
            unique.append(result)
        return unique

    matched = dedupe([r for r in report.results if r.candidate_id in known])
    if matched:
        return matched
    if not report.results:  # pragma: no cover - 防御性分支
        return []
    return dedupe(
        [
            result.model_copy(update={"candidate_id": candidate_id})
            for result, candidate_id in zip(report.results, candidate_ids)
        ]
    )


def _choose_focus(results: list[CriticResult], requested: str) -> CriticResult:
    """决定这一轮"该动谁"。

    优先级：模型指名的 → 要求改稿的那条 → 得分最高的那条。

    第二条不能省。如果只认 `focus_candidate_id`，模型一旦漏填，即使它对
    c2 明确说了 revise，流程也会聚焦到得分最高的 c1（decision=accept）上——
    于是 revise 永远不触发，整个改稿循环**静默失效**，而这不会有任何报错。
    """
    if requested:
        picked = next((r for r in results if r.candidate_id == requested), None)
        if picked is not None:
            return picked

    revising = [r for r in results if r.decision == "revise"]
    if revising:
        return revising[0]
    return max(results, key=lambda r: r.score)


def _apply_evidence_ceiling(
    results: list[CriticResult],
    angle: Any,
    candidates: list[Candidate],
    run_id: str,
) -> tuple[list[CriticResult], list[str]]:
    """把"证据托不住"的 accept 确定性地改判成 revise。

    返回 `(改写后的评价, 被拦下的 candidate_id)`。只动 `decision`、`flags` 与
    `revision_targets`——**分数一个都不改**。Critic 的分数是它对"这条稿子好不好"
    的判断，这里判的是"这条稿子现在能不能被接受"，两件事混在一起会让报告里
    看不出到底是稿子差还是证据薄。

    改写而不是丢弃：被拦下的那条仍然是候选里最好的一条，加一条 `revision_targets`
    就把它变成了可执行的改稿指令（"加对冲"或"补第二个独立来源"），
    而不是一句"重写"。
    """
    if angle is None or not results:
        return results, []

    assessment = getattr(angle, "assessment", None)
    bound_sources = getattr(assessment, "bound_sources", 0)
    bound_authors = getattr(assessment, "bound_authors", 0)
    content_of = {c.candidate_id: c.content for c in candidates}

    rewritten: list[CriticResult] = []
    blocked: list[str] = []
    for result in results:
        if result.decision != "accept":
            rewritten.append(result)
            continue
        allowed, why = viewpoint.accept_ceiling_for(
            content=content_of.get(result.candidate_id, ""),
            claim=angle.claim,
            bound_sources=bound_sources,
            bound_authors=bound_authors,
            stance=angle.stance,
            stance_declared=angle.stance_declared,
            max_stance=angle.max_stance,
        )
        if allowed:
            rewritten.append(result)
            continue
        blocked.append(result.candidate_id)
        logger.warning(
            "agent criticize refused accept (run_id=%s, candidate=%s): %s",
            run_id,
            result.candidate_id,
            why,
        )
        rewritten.append(
            result.model_copy(
                update={
                    "decision": "revise",
                    "flags": [*result.flags, "evidence_ceiling"],
                    "revision_targets": [*result.revision_targets, why],
                }
            )
        )
    return rewritten, blocked


def _apply_length_ceiling(
    results: list[CriticResult],
    candidates: list[Candidate],
    max_length: int,
    run_id: str,
) -> tuple[list[CriticResult], list[str]]:
    """把超过**交付硬上限**的 accept 确定性地改判成 revise。

    与 `_apply_evidence_ceiling` 同一个形状、同一个理由：`max_length` 是交付条件，
    不是打分项，所以它不能是"Critic 觉得要不要压短"的自由选择。模型读过算好的
    字数（`format_candidates` 把"超出上限 N 字"印在候选旁边），但它仍然可能放过
    一条超长的稿子——而这正是不能让"能不能交付"取决于模型自觉的地方。

    只动 `decision`、`flags` 与 `revision_targets`，**分数一个都不改**：分数是
    "这条稿子好不好"，超长是"这条稿子现在能不能交付"，两件事。一条写得很好但
    超长的稿子仍然是好稿子，它只是需要先压短。

    改判而不是丢弃：这条超长候选往往仍是池子里最好的一条，一条具体的
    `revision_targets`（"砍掉 N 字，不许删事实 / 删观点 / 改掉开头那个钩子"）
    就把它变成了可执行的改稿指令。**不在这里截断正文**——截断会造成句子残缺、
    观点失真、事实关系破坏，那是"看起来合规"的假象，比超长更糟。

    返回 `(改写后的评价, 被拦下的 candidate_id)`。
    """
    if not results:
        return results, []

    content_of = {c.candidate_id: c.content for c in candidates}
    rewritten: list[CriticResult] = []
    blocked: list[str] = []
    for result in results:
        if result.decision != "accept":
            rewritten.append(result)
            continue
        content = content_of.get(result.candidate_id, "")
        if not over_hard_limit(content, max_length):
            rewritten.append(result)
            continue
        blocked.append(result.candidate_id)
        logger.warning(
            "agent criticize refused accept (run_id=%s, candidate=%s): %s",
            run_id,
            result.candidate_id,
            length_detail(content, max_length),
        )
        rewritten.append(
            result.model_copy(
                update={
                    "decision": "revise",
                    "flags": [*result.flags, "too_long"],
                    "revision_targets": [
                        *result.revision_targets,
                        f"{length_detail(content, max_length)}，必须压到 "
                        f"{max_length} 字以内才能交付（这是硬上限，不是建议）。"
                        "压回的办法是删掉信息量最低的那几句或让句子更短，"
                        "**不许删事实、不许删观点、不许改掉开头那个钩子**。",
                    ],
                }
            )
        )
    return rewritten, blocked


def make_critic_tool(llm_service: LLMService, persona: Persona):
    async def criticize(state, decision) -> ToolResult:
        run_id = state["run_id"]
        candidates = list(state.get("candidates") or [])
        if not candidates:  # pragma: no cover - 前置条件已挡住，防御性分支
            return ToolResult(delta={}, summary="没有可评价的候选")

        angle = state.get("selected_angle")
        pack = state.get("evidence_pack")
        logger.info("agent criticize started (run_id=%s, candidates=%d)", run_id, len(candidates))

        prompt = render(
            load_prompt("critic"),
            GOAL=state.get("goal") or state.get("topic") or "",
            TOPIC=state.get("selected_topic") or "",
            # 与 Writer 拿到的是**同一份**背景渲染——两边看到的目标不同时，
            # Critic 会拿另一段背景去判"读者进不进得来"（同 VIEWPOINT / STYLE 的理由）。
            CONTEXT=format_context(getattr(pack, "context", None)),
            # 确定性陌生读者信号：由程序从**正文 + 背景 + 事实**算出，按需重算、
            # 不入库、不进 state。它只作为参考事实给模型，绝不混进 context_fit 的分数。
            CONTEXT_SIGNALS=format_context_signals(
                getattr(pack, "context", None), candidates, pack
            ),
            # 与 Writer 拿到的是**同一个**渲染（同一个函数、同一个 angle），
            # 否则 Critic 会拿另一个目标去判 viewpoint_consistency。
            VIEWPOINT=format_viewpoint(angle),
            # 确定性的范围信号，只是参考事实，绝不混进 overclaim_risk 那个分数。
            SCOPE_SIGNALS=format_scope_signals(angle),
            EVIDENCE=format_evidence(state.get("evidence_pack")),
            # 与 Writer 拿到的是**同一个**形状（同一个函数、同一份 state）。
            # 这不是"多给一点背景"——`factual_support` 要靠它判「正文里的数字
            # 是不是编的」：形状写着没有数字时，任何具体数字都是编造。
            # 两边形状不一致，Critic 会把一条被明确要求用数字写的正文判成造假。
            EVIDENCE_SHAPE=render_evidence_shape(
                evidence_shape(
                    state.get("evidence_pack"),
                    author_count=evidence_author_count(state),
                )
            ),
            # 三条候选本该走的三条路。Critic 靠它判「这条有没有做到它那条路」，
            # 与 Writer 读的是同一份渲染（同 VIEWPOINT / STYLE 的理由）。
            CANDIDATE_ROLES=render_candidate_roles(),
            PERSONA=format_persona(persona),
            MAX_LENGTH=persona.max_length,
            # 字数由程序算好递进来（见 format_candidates 的说明）：Critic 判
            # 「超上限」靠的是比较两个给定的数，不是目测中文字数。
            CANDIDATES=format_candidates(candidates, max_length=persona.max_length),
            # 确定性 AI 味信号：由程序从正文算出，**按需重算、不入库、不进 state**。
            # 它只作为参考事实给模型，绝不混进 generic_ai_style 那个分数。
            AI_SIGNALS=format_ai_signals(candidates),
            # 与 Writer 拿到的是**同一个** recipe（都在 state 里），
            # 否则 Critic 会拿另一个目标去判 style_fit。
            STYLE=format_style_recipe(state.get("style_recipe")),
            SOURCE_COUNT=len(sources(state)),
        )
        report: CriticReport = await complete_json(llm_service, prompt, CriticReport)

        results = _bind_to_candidates(report, [c.candidate_id for c in candidates])
        if not results:
            return ToolResult(delta={}, summary="Critic 没有给出可用的评价")

        focus = _choose_focus(results, report.focus_candidate_id)

        # 整批的差异观察（`CriticReport.candidate_distinctness`）。它是**观察**，
        # 不参与 decision，所以这里既不进 delta、也不改任何评价——只让它出现
        # 在人看得到的地方：日志、以及写进 decision_trace / agent_decision 的
        # 那行摘要。控制台因此能看到「这一批其实是一句话的三种措辞」，
        # 而不必去读三条正文自己比。
        #
        # 刻意**不**给它加 state 字段：那要动 checkpointer 的登记与 checkpoint
        # 形状，为一条纯观察付这个代价不值得。摘要这条路本来就在。
        if report.candidate_distinctness is not None:
            note = (report.distinctness_note or "").strip()
            logger.info(
                "agent criticize distinctness (run_id=%s, score=%.1f, note=%s)",
                run_id,
                report.candidate_distinctness,
                note or "（未说明）",
            )

        # 工作单第七节：**观点强度 ≤ 证据强度**。在此之前 accept 是 Critic 的
        # LLM 自由选择——`viewpoint.py` 算出的上界（立场上界、单一来源上限、
        # 动机判断的独立来源数）全都只是渲染给模型看的**参考事实**，模型说
        # accept，流程就走 accept。这里给"结论"加一个确定性上界，与
        # `max_stance_for` 对立场做的事完全一样：模型可以在上界之内更保守，
        # 不能更笃定。
        #
        # 检查**每一条** accept，不只是 focus：最终交给用户的那条是从
        # "decision=accept 的候选"里挑的（见 nodes/finalize.py），只改 focus
        # 会留下另一条没托住的 accept 顶上来——改判看着生效了，出稿还是那条。
        results, blocked = _apply_evidence_ceiling(results, angle, candidates, run_id)

        # 同一只手，另一件事：**交付硬上限**。长度与观点强度是两条独立的交付条件，
        # 一条不满足就不能交付，所以两道确定性上界都在这里过一遍。放在证据之后、
        # 是为了让下面那一段聚焦逻辑只写一次（它要同时看两组 blocked）。
        results, too_long = _apply_length_ceiling(
            results, candidates, persona.max_length, run_id
        )

        # 聚焦：这一轮改稿的机会只有一次（预算没变），要花在**真正卡住交付**的
        # 那条上。
        #
        # - 模型指的那条如果已经被改判，照旧聚焦它——它已经是一条可执行的 revise。
        # - 它如果仍然是 accept，而池子里有超长的 candidate，聚焦就换到超长的那条：
        #   否则这一轮 revise 会去改一条本来就没问题的稿子，把唯一的机会用完，
        #   超长的那条原封不动留给 finalize ——那时如果三条都超长，整个 Run 只能失败。
        # - 超长的候选里挑**最接近合格**的那条（超得最少），因为它是这一轮最可能
        #   一次性压回上限的；同样超得少时取分数高的（压完更可能是能交付的那条）。
        if too_long and focus.decision == "accept":
            shortest = min(
                (c for c in candidates if c.candidate_id in set(too_long)),
                key=lambda c: (over_hard_limit(c.content, persona.max_length), -max(
                    (r.score for r in results if r.candidate_id == c.candidate_id),
                    default=0.0,
                )),
            )
            moved = next(
                (r for r in results if r.candidate_id == shortest.candidate_id), None
            )
            if moved is not None:
                logger.info(
                    "agent criticize refocused on an overlong candidate "
                    "(run_id=%s, focus=%s → %s)",
                    run_id,
                    focus.candidate_id,
                    moved.candidate_id,
                )
                focus = moved
        else:
            focus = next(
                (r for r in results if r.candidate_id == focus.candidate_id), focus
            )

        logger.info(
            "agent criticize completed (run_id=%s, focus=%s, decision=%s)",
            run_id,
            focus.candidate_id,
            focus.decision,
        )

        # 整轮运行的结论累计。critic_results 每轮被整体覆盖（无 reducer 的 list
        # 字段），所以"这次运行里 Critic 一共 accept 了几条"只能靠这里的累计值，
        # 事后是反推不出来的。见 app/agent/state.py 与 app/agent/metrics.py。
        tally = dict(state.get("critic_tally") or {})
        for verdict in results:
            tally[verdict.decision] = tally.get(verdict.decision, 0) + 1

        delta = {
            "critic_results": results,
            "critic_result": focus,
            "critic_tally": tally,
            # 记下"评价的是哪一版候选池"，供 budget 判断是否需要再次评价。
            # 记成 **writer_epoch 的当前值**而不是自增计数：两次自增值只有在
            # 恰好同步时才正确，而 writer_epoch 是"被评价对象"的版本号，
            # 直接抄它就没有对不上的可能。
            "critic_epoch": counter(state, "writer_epoch"),
        }
        summary = (
            f"评价 {len(results)} 条："
            + "；".join(f"{r.candidate_id} {r.decision}({r.score:.1f})" for r in results)
            + f"。聚焦 {focus.candidate_id}"
        )
        if blocked:
            summary += (
                "。**观点强度超过证据强度**，这 %d 条原本被评成 accept，"
                "程序按「观点 ≤ 证据」改判为 revise：%s"
                % (len(blocked), "、".join(blocked))
            )
        if too_long:
            summary += (
                "。**超过交付硬上限 %d 字**，这 %d 条原本被评成 accept，"
                "程序改判为 revise：%s"
                % (persona.max_length, len(too_long), "、".join(too_long))
            )
        if focus.decision == "revise":
            summary += "，要求改稿：" + "；".join(focus.revision_targets or ["（未给出具体目标）"])
        if report.candidate_distinctness is not None:
            note = (report.distinctness_note or "").strip()
            summary += "。三条候选的差异度 %.1f/10" % report.candidate_distinctness
            if note:
                summary += f"：{note}"
            # 低分只**提示**，不加"必须改"的语气：它不构成改稿理由（见 prompt）。
            if report.candidate_distinctness <= 4:
                summary += "（偏低：这条不构成改稿理由，但值得看一眼三条是不是同一条）"
        return ToolResult(delta=delta, summary=summary)

    return criticize
