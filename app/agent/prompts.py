"""Prompt 模板的装载，以及把 state 里的结构渲染成人话。

与 Phase 1–4 的 `prompts/generate.txt` 同一套约定：模板放文件、用 `{{NAME}}` 占位。
渲染集中在这里，是因为 Agent 每一步都要把 persona / 证据 / 候选拼进 prompt——
七个模板各抄一份格式，改 persona 字段时必然漏掉一个。

**这里每个 format_* 都在做同一件事：把模型能引用的 id 印出来。**
Evidence 要引 research_item id、Angle 要引 fact_id、Critic 要引 candidate_id、
Planner 的 target 要引 topic / angle_id / candidate_id。模型只能引用它看得见的东西——
不把 id 印出来，它就只能编一个，而"编出来的 id"会让整条证据链在事后核对时对不上，
且不会有任何报错。
"""

from pathlib import Path
from typing import Any, Mapping

from app.agent.persona import Persona
from app.agent.schemas import (
    AgentAction,
    Candidate,
    CandidateTopic,
    CriticResult,
    EvidencePack,
)
from app.agent.schemas import DecisionTraceEntry
from app.config import settings
from app.style.labels import (
    density_label as _density_label,
    ending_label as _ending_label,
    evidence_label as _evidence_label,
    hook_label as _hook_label,
    stance_label as _stance_label,
    structure_label as _structure_label,
    tone_label as _tone_label,
)

# 标签表本身在 `app/style/labels.py`——Run Detail 要展示同一组取值，
# 抄一份就会漂，漂了还不报错。这里保留 `_xxx_label` 的私有别名，是为了让
# `format_style_recipe` 的正文一行都不用改。


def load_prompt(name: str) -> str:
    """读取 `prompts/agent/<name>.txt`。

    刻意不做缓存：这些文件在开发时会被反复改动，而缓存会让"改了 prompt 但没生效"
    变成一个很难查的问题。一次运行最多读十几次小文件，代价可以忽略。
    """
    return (Path(settings.agent_prompt_dir) / f"{name}.txt").read_text(encoding="utf-8")


def render(template: str, **values: object) -> str:
    """把 `{{KEY}}` 替换成对应值。

    不做模板引擎：占位符是固定的几个，引入 Jinja 只会多一个依赖和一层
    "为什么这里渲染出来是空的"的排查成本。
    """
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


def format_persona(persona: Persona) -> str:
    """persona 的文本块。

    与 Phase 2 的 generate 节点共用同一份实现（那边改为 import 本函数），
    否则 Agent 写出来的稿子和人工审核看到的人格设定可能不是同一套。
    """
    return (
        f"- 名称：{persona.name}\n"
        f"- 描述：{persona.description}\n"
        f"- 语气：{persona.tone}\n"
        f"- 风格：{'；'.join(persona.style)}\n"
        f"- 避免：{'；'.join(persona.avoid)}"
    )


def format_style_recipe(recipe: Any | None) -> str:
    """StyleRecipe 的文本块（Phase 5B.2）。

    **永远返回一段文本，绝不返回空串或 None**。两个理由：

    1. `render()` 只替换传进去的键，**不会因为缺键报错**——调用方若因为
       "这次没有 recipe"就跳过这个参数，字面量 `{{STYLE}}` 会原样进入发给
       模型的 prompt。所以调用方**必须**每次都传 `STYLE=format_style_recipe(...)`，
       而这个函数在无 recipe 时返回一段交代清楚的说明文字。
    2. 无 recipe 时返回"本次未指定写作结构"而不是空串：模型看到一片空白会
       自己补一套隐含的结构约束，而那是**不可见**的——它既不在 trace 里，
       也不在 Run Detail 里，事后无法解释稿子为什么长成那样。

    ## 这里印的是约束，不是样本

    recipe 里没有任何一条真实帖子的句子（工作单第二十二节的硬要求）。
    它只描述"该写成什么样"，不提供"照着抄的范本"。
    """
    if recipe is None:
        return (
            "（本次未指定写作结构。按上面的写作规则与既有风格自行组织，"
            "不要套用固定的段落模板。）"
        )

    lines = [
        f"- 结构：{_structure_label(recipe.structure_type)}",
        f"- 开头：{_hook_label(recipe.hook_type)}",
    ]
    # 长度是这个 prompt 里**唯一**有两个来源的参数：语料观察 + 平台上限。
    # 两者的关系必须在给模型的文本里写清楚，否则模型只看到一个 140，
    # 会把它当成语料的结论去执行——"这些人平时写 140 字"，而事实是
    # "语料说 180，我们只能发 140"。前者会让它继续往短里压缩内容，
    # 后者才让它知道该保留多少信息量。
    length = recipe.effective_target_length
    if length:
        if recipe.is_length_capped:
            lines.append(
                f"- 目标长度：**不超过 {length} 字**。"
                f"（语料里典型长度是 {recipe.observed_target_length} 字，"
                f"受发布长度硬上限约束，本次实际写到 {length} 字左右即可；"
                f"**这不是语料认为该写多短**，所以不要为了凑短而丢掉事实。）"
            )
        else:
            lines.append(f"- 目标长度：约 {length} 字")
    lines.append(f"- 信息密度：{_density_label(recipe.density)}")
    lines.append(f"- 立场强度：{_stance_label(recipe.stance_strength)}")
    lines.append(f"- 证据要求：{_evidence_label(recipe.evidence_requirement)}")
    lines.append(f"- 语气：{_tone_label(recipe.tone)}")
    lines.append(f"- 结尾：{_ending_label(recipe.ending_type)}")

    body = "\n".join(lines)
    if recipe.reasons:
        # 把依据一并给模型：知道"为什么是这个结构"比只看到结构名更能约束写法，
        # 也让人在事后能从 prompt 里看出这个选择从哪来。
        rationale = "\n".join(f"  - {reason}" for reason in recipe.reasons)
        return f"{body}\n选择依据：\n{rationale}"
    return body


def format_sources(sources: list[tuple[str, str]]) -> str:
    """研究材料列表，形如 `[<research_item id>] 正文`。

    `sources` 是 (research_item.id, content) 对，由 `tools/base.py::append_research`
    统一构造与追加——两个列表只由那一个函数改动，所以下标恒一一对应。
    """
    if not sources:
        return "（无研究材料）"
    return "\n".join(f"[{item_id}] {content}" for item_id, content in sources)


def format_evidence(pack: EvidencePack) -> str:
    """证据包列表（f1、f2…）。Angle 按 fact_id 引用它们。"""
    if not pack.facts:
        return "（无证据）"
    lines = [
        f"[{fact.fact_id}] {fact.fact}（出处：{', '.join(fact.source_ids) or '无'}）"
        for fact in pack.facts
    ]
    for label, items in (
        ("共识", pack.consensus),
        ("反方", pack.counterpoints),
        ("未知", pack.unknowns),
    ):
        if items:
            lines.append(f"{label}：{'；'.join(items)}")
    return "\n".join(lines)


def format_candidates(candidates: list[Candidate]) -> str:
    if not candidates:
        return "（无候选）"
    return "\n\n".join(
        f"[{c.candidate_id}]（{c.label}，第 {c.revision} 稿）\n{c.content}" for c in candidates
    )


def format_critics(results: list[CriticResult]) -> str:
    if not results:
        return "（尚未评价）"
    return "\n".join(
        f"[{r.candidate_id}] {r.decision} score={r.score} "
        f"flags={r.flags} revision_targets={r.revision_targets}"
        for r in results
    )


# 每个动作一句话说明，供 Planner 的 prompt 渲染。
#
# 与 AgentAction 枚举放在一起会更好，但那是 schema，不该混入 prompt 文案。
# 这里漏掉一个动作时会被 test_planner_prompt_covers_every_action 抓到。
ACTION_HINTS: dict[AgentAction, str] = {
    AgentAction.DISCOVER_TOPICS: "从目标出发检索最近的材料，发现若干候选选题并打分",
    AgentAction.SELECT_TOPIC: "在候选选题里选定一个（target 填 topic_id 或标题原文）",
    AgentAction.RESEARCH_TOPIC: "围绕某个候选选题取回材料（target 填候选选题原文）",
    AgentAction.DEEPEN_RESEARCH: "针对已知的空白再检索一轮（arguments 里给一个不同的检索词）",
    AgentAction.EXTRACT_EVIDENCE: "把已有材料提炼成可追溯的证据包",
    AgentAction.GENERATE_ANGLES: "基于证据提出若干观点",
    AgentAction.SELECT_ANGLE: "在已生成的观点里选定一个（target 填 angle_id）",
    AgentAction.GENERATE_CANDIDATES: "按选定观点写出候选正文",
    AgentAction.CRITICIZE: "评价候选正文，指出问题（不会改稿）",
    AgentAction.REVISE: "按评审意见改一条候选（target 填候选 id）",
    AgentAction.FINISH: "结束探索，把手上的候选交给护栏与人工审核",
}


def format_allowed(actions: tuple[AgentAction, ...]) -> str:
    """当前可选动作清单。

    这是"纸面选项"的**唯一**来源：`allowed_actions` 算出来的同一份元组，
    既印进 prompt，也用于校验模型的决策。两者同源，就不会出现
    "prompt 里说可以、实际被拦下"的漂移。
    """
    return "\n".join(f"- {action.value}：{ACTION_HINTS.get(action, '')}" for action in actions)


def format_budget(state: Mapping[str, Any], budget) -> str:
    """预算余量。用"还剩几次"而不是"用了多少"，因为 Planner 要判断的是还能不能做。"""
    return (
        f"- 步数：还剩 {max(0, budget.max_steps - _int(state, 'step_count'))} 步"
        f"（上限 {budget.max_steps}）\n"
        f"- 检索：还剩 {max(0, budget.max_research_calls - _int(state, 'research_count'))} 次"
        f"（上限 {budget.max_research_calls}）\n"
        f"- 改稿：还剩 {max(0, budget.max_revisions - _int(state, 'revision_count'))} 次"
        f"（上限 {budget.max_revisions}）\n"
        f"- 候选总数上限 {budget.max_candidates} 条"
    )


def _int(state: Mapping[str, Any], key: str) -> int:
    value = state.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _topics(topics: list[CandidateTopic]) -> str:
    """候选选题摘要：编号、分数、分档，**不带模型的自述**。

    `why_now` 之类是模型写的解释，不印进 Planner 的 state 摘要：Planner 要做的是
    从候选里选一个，而选哪个已经有 `trend_score` 这个确定性的依据。
    把解释塞进来只会让"说得最漂亮的那个"看起来像最该选的那个。

    分档（qualification）必须印：没有它，Planner 看不出"这条只有一位作者参与、
    分数是被封顶过的"，而 `select_topic` 恰恰会优先选 qualified 的，
    两边看到的东西不一致时，回退就会显得莫名其妙。
    """
    if not topics:
        return "（尚未发现）"
    return "；".join(
        f"{t.topic_id} {t.title}（{t.trend_score:.1f} 分/{t.qualification.value}，"
        f"{t.author_count} 位作者）"
        for t in topics
    )


def format_state(state: Mapping[str, Any]) -> str:
    """把 state 渲染成 Planner 能读的进展摘要。

    只印**决策需要看到的**东西：有几个候选选题、材料够不够、证据里有多少事实与
    空白、观点选了哪个、候选写成什么样、评审说了什么。正文与材料原文不印——
    它们又长又与"下一步做什么"无关，塞进来只会稀释判断依据。
    """
    pack = state.get("evidence_pack")
    facts = getattr(pack, "facts", None) or []
    unknowns = getattr(pack, "unknowns", None) or []
    angles = state.get("angles") or []
    angle = state.get("selected_angle")
    candidates = state.get("candidates") or []
    critic = state.get("critic_result")

    lines = [
        f"- 候选选题：{len(state.get('candidate_topics') or [])} 个"
        f"（按 trend_score 降序，t1 是分最高的）"
        f" —— {_topics(state.get('candidate_topics') or [])}",
        f"- 已选定题：{state.get('selected_topic') or '（未选定）'}",
        f"- 研究材料：{len(state.get('research_item_ids') or [])} 条",
        f"- 证据：{len(facts)} 条事实，{len(unknowns)} 处未知"
        + (f"，未知例如：{unknowns[0]}" if unknowns else ""),
        f"- 观点：已生成 {len(angles)} 个，"
        + (f"当前选定 {angle.angle_id}：{angle.claim}" if angle is not None else "尚未选定"),
    ]

    if candidates:
        lines.append(
            "- 候选正文："
            + "；".join(f"{c.candidate_id}（{c.label}，第 {c.revision} 稿，{len(c.content)} 字）" for c in candidates)
        )
    else:
        lines.append("- 候选正文：（尚未写出）")

    if critic is not None:
        lines.append(
            f"- 最近一次评审：聚焦 {critic.candidate_id}，结论 {critic.decision}"
            f"（{critic.score:.1f} 分）"
            + (f"，要求改动：{'；'.join(critic.revision_targets)}" if critic.revision_targets else "")
        )
    elif candidates and _int(state, "writer_epoch") > _int(state, "critic_epoch"):
        # 改稿会清空 critic_result（见 tools/revise.py）。此时"没有聚焦对象"**不等于**
        # "从没评价过"——渲染成后者会让 Planner 以为该从头重来，而它真正该做的
        # 只有一件事：重新评价刚改过的那批候选。
        lines.append("- 最近一次评审：候选改过稿，上一轮评价已作废，需要重新评价")
    else:
        lines.append("- 最近一次评审：（尚未评价）")

    return "\n".join(lines)


def format_trace(trace: list[DecisionTraceEntry]) -> str:
    """已做过的决策。Planner 靠它避免重复劳动，人靠它理解轨迹。"""
    if not trace:
        return "（这是第一步）"
    return "\n".join(
        f"{e.step}. {e.action}"
        + (f"（{e.target}）" if e.target else "")
        + f" —— {e.reason}"
        + (f" → {e.result_summary}" if e.result_summary else "")
        for e in trace
    )
