"""criticize_candidate：评价候选，**不改稿**（工作单第十二节）。

这条边界是 Critic 存在的全部意义。一个既能评价又能改稿的 Critic，实际上是一个
没有约束的 Writer：它会把"这地方说得不够狠"直接变成"我替你写狠一点"，
于是评价与写作互相掩盖，出了问题分不清是判断错了还是笔头错了。

所以这里只有一次 LLM 调用、产出结构化评价，正文一个字都不动。

评价是**整批一次**的（见 `CriticReport` 的说明）。三条候选的差别在写法上，
分开评价等于让 Critic 在自己看不见另外两条的情况下回答"哪条更适合这个角度"。
"""

import logging

from app.agent.budget import counter
from app.agent.persona import Persona
from app.agent.prompts import (
    format_candidates,
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


def make_critic_tool(llm_service: LLMService, persona: Persona):
    async def criticize(state, decision) -> ToolResult:
        run_id = state["run_id"]
        candidates = list(state.get("candidates") or [])
        if not candidates:  # pragma: no cover - 前置条件已挡住，防御性分支
            return ToolResult(delta={}, summary="没有可评价的候选")

        angle = state.get("selected_angle")
        logger.info("agent criticize started (run_id=%s, candidates=%d)", run_id, len(candidates))

        prompt = render(
            load_prompt("critic"),
            GOAL=state.get("goal") or state.get("topic") or "",
            TOPIC=state.get("selected_topic") or "",
            # 与 Writer 拿到的是**同一个**渲染（同一个函数、同一个 angle），
            # 否则 Critic 会拿另一个目标去判 viewpoint_consistency。
            VIEWPOINT=format_viewpoint(angle),
            # 确定性的范围信号，只是参考事实，绝不混进 overclaim_risk 那个分数。
            SCOPE_SIGNALS=format_scope_signals(angle),
            EVIDENCE=format_evidence(state.get("evidence_pack")),
            PERSONA=format_persona(persona),
            MAX_LENGTH=persona.max_length,
            CANDIDATES=format_candidates(candidates),
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
        if focus.decision == "revise":
            summary += "，要求改稿：" + "；".join(focus.revision_targets or ["（未给出具体目标）"])
        return ToolResult(delta=delta, summary=summary)

    return criticize
