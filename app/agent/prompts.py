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

import logging
from pathlib import Path
from typing import Any, Mapping

from app.agent.labels import (
    claim_scope_label as _claim_scope_label,
    context_origin_label,
    viewpoint_stance_label as _viewpoint_stance_label,
)
from app.agent.persona import Persona
from app.agent.schemas import (
    AgentAction,
    Candidate,
    CandidateTopic,
    CriticResult,
    EvidencePack,
)
from app.agent.schemas import DecisionTraceEntry
from app.agent.viewpoint import TIE_MARGIN
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

logger = logging.getLogger(__name__)


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


#: `Voice` 各段的显示标题，顺序即渲染顺序。
#: 用字段名而不是在渲染函数里写 `if`，是为了加一段新契约时只改这张表。
_VOICE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("structure", "结构"),
    ("hook_rules", "开头"),
    ("rhythm_rules", "节奏"),
    ("plain_language", "说人话"),
    ("allowed_devices", "允许的手法"),
    # 结尾排在事实红线**前面**：红线读到最后一条的印象最深，把它留在末尾。
    ("ending_rules", "结尾"),
    ("fabrication_red_lines", "事实红线"),
)


def format_persona(persona: Persona) -> str:
    """persona 的文本块。

    与 Phase 2 的 generate 节点共用同一份实现（那边改为 import 本函数），
    否则 Agent 写出来的稿子和人工审核看到的人格设定可能不是同一套。

    这一块会**同时**进入 writer（candidates / revise / generate）与 critic 的
    prompt，因为它们共用 `{{PERSONA}}`。所以阅读契约只写在这里一次，写作端与
    评审端看到的就是同一条线——这正是让 critic 站在新声音这一边、而不是继续给
    「情绪化 / 标题党」扣分的最短路径。

    `voice` 是可选的：旧 persona 没有这一段时，渲染结果与从前一致（只是不再有
    「避免」一行）。
    """
    lines = [
        f"- 名称：{persona.name}",
        f"- 描述：{persona.description}",
        f"- 语气：{persona.tone}",
        f"- 风格：{'；'.join(persona.style)}",
    ]
    voice = persona.voice
    if voice is not None:
        # 每条独立成行，而不是用「；」挤成一段：契约是要被**照着写**的，
        # 一段长句里夹着五六条规则时，模型只会抓住最后半条。
        for field, title in _VOICE_SECTIONS:
            items = getattr(voice, field)
            if not items:
                continue
            lines.append(f"- {title}：")
            lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines)


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
        # 这段说的是**形状**（结构 / 长度 / 密度 / 证据要求），来自语料观察。
        # 开头两行先把从属关系钉死：语气与段落节奏由上面的人格 / 阅读契约决定，
        # recipe 不参与——否则同一个 prompt 里会同时出现"分析型"和"像跟朋友聊天"，
        # 模型只能二选一，而选哪一条是随机的。
        "- 以下是语料观察到的结构参数，只约束**形状**（结构 / 长度 / 密度 / 证据要求）。",
        "- 与上面的「阅读契约」冲突时，**一律以阅读契约为准**：形状可以让步，声音不让。",
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
                f"受我们的写作长度目标约束，本次实际写到 {length} 字左右即可；"
                f"**这不是语料认为该写多短**，所以不要为了凑短而丢掉事实。）"
            )
        else:
            lines.append(f"- 目标长度：约 {length} 字")
    lines.append(f"- 信息密度：{_density_label(recipe.density)}")
    lines.append(f"- 立场强度：{_stance_label(recipe.stance_strength)}")
    lines.append(f"- 证据要求：{_evidence_label(recipe.evidence_requirement)}")
    # 语气这一行是**从属**的：它来自语料，而"用什么姿态说话"由阅读契约定。
    # 标成"语料显示的语气倾向"并写明服从关系，模型才不会把语料的观察当成
    # 一条必须执行的语气指令——那会把它拉回"分析型"的腔调。
    lines.append(
        f"- 语料显示的语气倾向：{_tone_label(recipe.tone)}"
        "（**从属**：与上面的「阅读契约」冲突时以契约为准，"
        "它不改变段落节奏与说话姿态）"
    )
    lines.append(f"- 结尾：{_ending_label(recipe.ending_type)}")

    body = "\n".join(lines)
    if recipe.reasons:
        # 把依据一并给模型：知道"为什么是这个结构"比只看到结构名更能约束写法，
        # 也让人在事后能从 prompt 里看出这个选择从哪来。
        rationale = "\n".join(f"  - {reason}" for reason in recipe.reasons)
        return f"{body}\n选择依据：\n{rationale}"
    return body


#: 渲染给 Writer 的反馈摘要长度上限（字符）。
#:
#: 不是为了省令牌，是为了让 Writer 看到的是一段**能读完**的背景——
#: 一屏塞不下的历史会诱导模型去抓它抓得住的那一条（通常是最大的那个数），
#: 而那正是工作单第四十二节要避免的"把某一条观察当成目标"。
MAX_FEEDBACK_CONTEXT_CHARS = 1200


def format_feedback_context(context: Any | None) -> str:
    """FeedbackContext 的文本块（Phase 5B.4）。

    ## 永远返回一段文本

    同 `format_style_recipe`：`render()` 不会因为缺键报错，调用方漏传这个参数，
    字面量 `{{FEEDBACK}}` 会原样进入发给模型的 prompt。所以**无数据时也要有话说**
    ——返回的是"还没有采集到历史结果"这句交代，而不是空串。

    ## 它是**观察**，不是指令（工作单第四十二节）

    这段文本里没有一条过去帖子的正文，也没有"这种写法拿到了多少赞"这类
    可以被当成目标的说法。它只有样本量、几类互动的分布、以及这份摘要的局限。
    渲染完再过一遍 `assert_observational`：这些话是代码用固定模板生成的，
    命中指令性措辞只有一个解释——有人改了模板。

    ## 守卫失败时不炸掉运行

    反馈是增强，不是前置条件（工作单第三十八节）。万一守卫真的命中了，
    这里记一条 error 并把摘要丢掉，而不是让 `{{FEEDBACK}}` 的渲染抛异常
    把整个 Run 带走——**一次措辞失误不该让 Agent 停下来**。
    """
    if context is None or getattr(context, "is_empty", True):
        return (
            "（本次没有可用的历史结果观察：账号此前发布的内容还没有被采集过，"
            "或采集到的数量还不构成任何可说的分布。按上面的目标与证据自行写作。）"
        )

    lines = [
        "- 以下是你自己账号**过去已发布内容**被观察到的结果，只作背景参考，"
        "不是本次的写作要求。",
        f"- 样本：{context.snapshot_count} 条观察，覆盖 {context.post_count} 条已发布内容。",
    ]
    latest = getattr(context, "latest_observed_at", None)
    if latest is not None:
        lines.append(f"- 最近一次观察时刻：{latest.isoformat()}。")
    for signal in context.signals:
        lines.append(f"- {signal.statement}")
    if context.limitations:
        lines.append("- 口径与局限：" + "；".join(context.limitations))

    text = "\n".join(lines)
    if len(text) > MAX_FEEDBACK_CONTEXT_CHARS:
        text = text[:MAX_FEEDBACK_CONTEXT_CHARS].rstrip() + "…（已达长度上限，此处截断）"

    try:
        from app.feedback.signals import assert_observational

        assert_observational(text, where="feedback context prompt block")
    except Exception:  # noqa: BLE001 - 见 docstring：措辞问题不该让运行失败
        logger.error("feedback context failed the observational guard; dropping it", exc_info=True)
        return (
            "（本次的反馈摘要未通过观察性检查，已丢弃。"
            "按上面的目标与证据自行写作。）"
        )
    return text


#: 渲染给 Writer 的学习准备度摘要长度上限（字符）。理由同
#: `MAX_FEEDBACK_CONTEXT_CHARS`：一屏塞不下的历史会诱导模型去抓最大的那个数。
MAX_LEARNING_CONTEXT_CHARS = 1200

#: 摘要里最多列出几条比较结论。字符上限之外再加一个条数上限：一条陈述的长度
#: 是可变的，只靠字符截断会让"看到几条"随措辞变化。**确定性**优先。
MAX_LEARNING_CONTEXT_COMPARISONS = 6


def format_learning_context(report: Any | None) -> str:
    """LearningReport 的文本块（Phase 5C 工作单第二十一至二十三节）。

    ## 本阶段**没有**把它接进任何模板

    这个函数是一段**接口**：写好了、有守卫、有测试，但 `prompts/agent/*.txt`
    里没有 `{{LEARNING}}`，候选工具也不会去组装它（工作单第二十一节允许
    Feedback Context 升级，第三十五节要求输入优先级不变）。原因很简单——
    现在真实数据是 n=1，接进去只会让 Writer 每轮读到一段"目前还不能学"，
    那不提供任何信息，只消耗上下文。

    ## 它说的仍然是**观察**

    输出的每一句都过 `assert_observational` 与 `assert_no_causal_claim`：
    这里没有"应该怎么写"，只有"在当前样本里观察到什么差异"，外加一句
    明确的"这不能用来指导写作"。守卫失败时丢掉整段并记一条 error，
    姿态同 `format_feedback_context`——措辞问题不该让一次 Run 停下。
    """
    if report is None:
        return (
            "（本次没有可用的学习准备度观察：还没有足够的已发布内容被观察，"
            "或该次读取没有产出报告。按上面的目标与证据自行写作。）"
        )

    readiness = getattr(report, "readiness", None)
    if readiness is None or not getattr(report, "records", ()):
        return (
            "（本次没有可用的学习准备度观察：还没有任何已发布内容可供观察。"
            "按上面的目标与证据自行写作。）"
        )

    verdict = "已具备" if readiness.learning_ready else "尚不具备"
    lines = [
        "- 以下是对**过去已发布内容**的跨帖观察汇总，只作背景参考："
        "它不是写作要求，也不是事实断言，更不保证任何结果。",
        f"- 学习准备度：{verdict}（原因：{readiness.reason}）。"
        f"观察窗口 {readiness.observation_window}，"
        f"有读数的帖子 {readiness.sample_count} 条。",
    ]
    comparisons = [c for c in getattr(report, "comparisons", ()) if c.valid]
    for comparison in comparisons[:MAX_LEARNING_CONTEXT_COMPARISONS]:
        lines.append(f"- {comparison.statement}")
    if len(comparisons) > MAX_LEARNING_CONTEXT_COMPARISONS:
        lines.append(
            f"- （另有 {len(comparisons) - MAX_LEARNING_CONTEXT_COMPARISONS} 条"
            "比较未列出。）"
        )
    if not comparisons:
        # 措辞避开因果连词：这一段本身要过 `assert_no_causal_claim`，
        # 而"因此"正是那张表里的一个词。
        lines.append("- 当前样本不足以构成任何一组可比较的分组，没有差异可列。")
    if readiness.limitations:
        lines.append("- 口径与局限：" + "；".join(readiness.limitations))

    text = "\n".join(lines)
    if len(text) > MAX_LEARNING_CONTEXT_CHARS:
        text = text[:MAX_LEARNING_CONTEXT_CHARS].rstrip() + "…（已达长度上限，此处截断）"

    try:
        from app.feedback.signals import assert_no_causal_claim, assert_observational

        assert_observational(text, where="learning context prompt block")
        assert_no_causal_claim(text, where="learning context prompt block")
    except Exception:  # noqa: BLE001 - 见 docstring：措辞问题不该让运行失败
        logger.error(
            "learning context failed the observational guard; dropping it", exc_info=True
        )
        return (
            "（本次的学习准备度摘要未通过观察性检查，已丢弃。"
            "按上面的目标与证据自行写作。）"
        )
    return text


#: 提案预览的长度上限与条数上限。**条数上限比提案总量还小**（生成阶段最多 3 条，
#: 见 `app/learning/proposal.py::MAX_PROPOSALS`）：一个只给人看的预览没有理由
#: 比"系统最多愿意提几条"还长，而两个上限都在这里写死，是为了让输出长度
#: 与措辞无关——确定性优先（同 `MAX_LEARNING_CONTEXT_COMPARISONS`）。
MAX_PROPOSAL_PREVIEW_CHARS = 1200
MAX_PROPOSAL_PREVIEW_ITEMS = 3


def format_proposal_preview(
    proposals: Any | None, *, learning_ready: bool = False
) -> str:
    """提案的文本预览（Phase 5D 工作单第二十五节）。

    ## 本阶段**没有**把它接进任何模板，而且**接不进去**

    这个函数是一段**接口**：写好了、有守卫、有测试，但 `prompts/agent/*.txt`
    里**没有** `{{PROPOSALS}}`，候选工具也不会去组装它。它的三个合法用途是：

        提案预览（人看的） / 人工审核时对照 / 离线评测

    它**不是**给 Writer 的输入。一条"可以尝试把开头方式改成 question"的建议
    一旦进入写作 prompt，就等于系统绕过了人工审核去改变自己的行为——而那正是
    工作单第一条禁止的事。所以这里连函数名都叫 `preview`：它产出的是**给人
    看的文本**，不是给模型看的指令。

    ## 闸门关着时它说"零条"，不说"没有提案"

    两者不同：`learning_ready=False` 是"算过了，不该提"；没有提案是"这次没算出
    可提的东西"。输出里必须能区分，否则读者会把"数据还不够"读成"看过了，没
    发现什么"。

    ## 措辞走的是与提案生成**同一套**守卫

    输出的每一句都过 `assert_observational` 与 `assert_no_causal_claim`，
    并且逐条写出样本量与局限。守卫失败时丢掉整段并记一条 error——姿态同
    `format_feedback_context`：措辞问题不该让一次 Run 停下。
    """
    items = tuple(proposals or ())
    if not learning_ready:
        return (
            "（学习闸门当前未开，本次不产生任何提案。"
            "这不是「没算出东西」，是「数据还不够可靠，因此不提建议」。）"
        )
    if not items:
        return "（学习闸门已开，但本次没有产生任何提案：没有可成立的分组比较。）"

    lines = [
        "- 以下是对**过去已发布内容**的跨帖观察推出的一些**可以尝试的方向**："
        "它不是写作要求，不是事实断言，也不保证任何结果；"
        "系统不会自动采用其中任何一条。"
    ]
    for proposal in items[:MAX_PROPOSAL_PREVIEW_ITEMS]:
        target = getattr(proposal, "target", "")
        current = getattr(proposal, "current_value", "")
        proposed = getattr(proposal, "proposed_value", "")
        sample = getattr(proposal, "sample_count", 0)
        window = getattr(proposal, "observation_window", "")
        evidence = len(getattr(proposal, "evidence_ids", ()) or ())
        lines.append(
            f"- 目标 {target}：当前 {current}，值得尝试 {proposed}"
            f"（依据 {evidence} 条证据 / {sample} 条帖子 / 窗口 {window}）。"
        )
        limitations = getattr(proposal, "limitations", ()) or ()
        if limitations:
            # 去掉每条末尾的句号再拼：模板里的局限句自带"。"，直接 join 会出现
            # "…结论。；点赞…"这种读起来像排版事故的东西。
            lines.append(
                "  - 局限：" + "；".join(item.rstrip("。") for item in limitations) + "。"
            )
    if len(items) > MAX_PROPOSAL_PREVIEW_ITEMS:
        lines.append(
            f"- （另有 {len(items) - MAX_PROPOSAL_PREVIEW_ITEMS} 条提案未列出。）"
        )

    text = "\n".join(lines)
    if len(text) > MAX_PROPOSAL_PREVIEW_CHARS:
        text = text[:MAX_PROPOSAL_PREVIEW_CHARS].rstrip() + "…（已达长度上限，此处截断）"

    try:
        from app.feedback.signals import assert_no_causal_claim, assert_observational

        assert_observational(text, where="proposal preview prompt block")
        assert_no_causal_claim(text, where="proposal preview prompt block")
    except Exception:  # noqa: BLE001 - 见 docstring：措辞问题不该让运行失败
        logger.error(
            "proposal preview failed the observational guard; dropping it", exc_info=True
        )
        return "（本次的提案预览未通过观察性检查，已丢弃。）"
    return text


def format_viewpoint(angle: Any | None) -> str:
    """一个观点的文本块，给 Writer 与 Critic 共用（Phase 5B.3）。

    **永远返回一段文本，绝不返回空串或 None**——理由与 `format_style_recipe`
    完全相同：`render()` 不会因为缺键报错，调用方漏传这个参数，字面量
    `{{VIEWPOINT}}` 会原样进入发给模型的 prompt。

    **Writer 与 Critic 必须看到同一份渲染**。分开写两份的话，Critic 会拿一个
    与 Writer 不同的目标去判"有没有写对"，而两边都自认为正确——
    Phase 5B.2 的 `{{STYLE}}` 已经踩过一次这个坑。

    ## 这里没有分数

    `viewpoint_score` / `confidence` 一律不印：分数决定"选哪一个"，
    不决定"怎么写"。把它们印进 prompt，模型会去迎合分数——而分数衡量的是
    证据结构，不是措辞，迎合的结果是正文去重复事实以显得"支撑充分"。
    证据结构与范围的**参考信号**另走 `format_scope_signals`，只给 Critic。
    """
    if angle is None:
        return "（本次未选定观点。按上面的目标自行判断该说什么——但不要编造事实。）"

    lines = [f"**判断**：{angle.claim}"]

    reasoning = getattr(angle, "reasoning", "") or ""
    if reasoning:
        lines.append(f"**推理**：{reasoning}")

    why = getattr(angle, "why_it_matters", "") or ""
    if why:
        lines.append(f"**为什么重要**：{why}")

    evidence_ids = getattr(angle, "evidence_ids", None) or []
    if evidence_ids:
        lines.append(
            f"**依据**：{('、'.join(evidence_ids))}"
            "（只依据这些事实；引用别的编号即为编造）"
        )
    else:
        lines.append("**依据**：（没有绑定事实——正文不得把这个判断说成有据可依）")

    counterpoints = getattr(angle, "counterpoints", None) or []
    if counterpoints:
        bound = set(getattr(angle, "counterpoint_evidence_ids", None) or [])
        rendered = "；".join(
            f"{point}（依据：{('、'.join(sorted(bound))) or '未绑定事实'}）"
            for point in counterpoints
        )
        lines.append(f"**反方**：{rendered}")
    else:
        lines.append("**反方**：（没有回应任何相反说法——正文不要写成没有争议）")

    assumptions = getattr(angle, "assumptions", None) or []
    if assumptions:
        lines.append(f"**依赖的假设**：{'；'.join(assumptions)}")

    implications = getattr(angle, "implications", None) or []
    if implications:
        lines.append(f"**由此推出的影响**：{'；'.join(implications)}")

    risk = getattr(angle, "risk", "") or ""
    if risk:
        lines.append(f"**可能错在**：{risk}")

    scope = getattr(angle, "claim_scope", None)
    stance = getattr(angle, "stance", None)
    boundary = []
    if scope is not None:
        boundary.append(f"适用范围：{_claim_scope_label(scope)}")
    if stance is not None:
        boundary.append(f"立场强度：{_viewpoint_stance_label(stance)}")
    if boundary:
        lines.append(f"**边界**：{'；'.join(boundary)}")

    return "\n".join(lines)


def format_scope_signals(angle: Any | None) -> str:
    """观点的确定性参考信号，**只给 Critic**（Phase 5B.3）。

    与 `format_signals`（AI 味信号）同一条纪律：**只作为参考事实呈现，
    绝不混进模型自己的打分，也不做门禁**。Critic 看到"证据范围是 case、
    申报范围是 industry"是**有助于**它判断 overclaim 的；把这个差值做成
    一个扣分项，则会得到一个无法复核的暗分——而且模型会学会绕过它。

    判不出来的项写"未评估"，不写 0。
    """
    if angle is None:
        return "（本次未选定观点，没有可参考的范围信号。）"

    assessment = getattr(angle, "assessment", None)
    if assessment is None:
        return (
            "（这个观点来自 Phase 5B.3 之前的运行，没有确定性评估明细；"
            "下面的判断只能基于正文本身。）"
        )

    lines: list[str] = []
    evidence_scope = getattr(angle, "evidence_scope", None)
    declared_scope = getattr(angle, "claim_scope", None)
    if evidence_scope is not None:
        lines.append(
            f"- 证据能支撑的范围：{_claim_scope_label(evidence_scope)}"
            f"（{assessment.bound_facts} 条事实、{assessment.bound_sources} 条来源、"
            f"{assessment.bound_authors} 位作者）"
        )
    lines.append(
        "- 观点申报的范围："
        + (_claim_scope_label(declared_scope) if declared_scope is not None else "未申报")
    )
    markers = list(assessment.breadth_markers or [])
    lines.append(
        "- 正文里的全称量词："
        + ("、".join(markers) if markers else "无")
    )
    if getattr(angle, "scope_exceeds_evidence", None):
        lines.append(
            "- **范围超出证据**：主张的范围比证据能支撑的更宽。这属于 overclaim，"
            "按 P0/P1 处理。"
        )

    # 动机/意图断言（工作单第六节）：这一栏是**闸门**，不是参考——程序会用它
    # 决定 accept 能不能成立（viewpoint.accept_ceiling_for）。所以这里必须写
    # 清楚「程序查到了什么、会据此怎么判」，否则模型会以为自己判 accept 就是最终
    # 结论，而它的结论随后被静默改判。
    motive_markers = list(getattr(assessment, "motive_markers", None) or [])
    if motive_markers:
        lines.append(
            f"- 正文/判断里的**动机或意图断言**：{'、'.join(motive_markers)}"
        )
    if getattr(assessment, "motive_unsupported", False):
        lines.append(
            "- **动机判断没有独立来源托住**：互相独立的来源不足 2 条。材料证实得了"
            "「做了什么」，证实不了「为了什么」。这一条**程序会拦住 accept**，"
            "除非正文把它写成了对冲过的解读（更像 / 可能 / 一种解读 / 从目前材料看）"
            "——所以这一项应当按 P0/P1 判，并把「改成解读」写进 revision_targets。"
        )
    lines.append(
        f"- 反方回应：声明 {assessment.declared_counterpoints} 条、"
        f"其中 {assessment.bound_counterpoints} 条绑定到事实"
    )
    if assessment.restates_fact:
        lines.append("- **判定为事实复述**：正文与某条事实有 12 字以上重合。")
    if assessment.originality_claim:
        lines.append(
            "- **无法核验的原创性表述**：正文用了「全网/首创」一类措辞。"
            "本系统没有全网语义检索能力，这类表述不予采信。"
        )
    if markers:
        lines.append("（命中词逐字回传是为了便于核对误报，不是自动判负。）")
    return "\n".join(lines)


def format_sources(sources: list[tuple[str, str]]) -> str:
    """研究材料列表，形如 `[<research_item id>] 正文`。

    `sources` 是 (research_item.id, content) 对，由 `tools/base.py::append_research`
    统一构造与追加——两个列表只由那一个函数改动，所以下标恒一一对应。
    """
    if not sources:
        return "（无研究材料）"
    return "\n".join(f"[{item_id}] {content}" for item_id, content in sources)


def format_context(context: Any | None, *, for_reader: bool = True) -> str:
    """话题背景块的文本（工作单第三、四、六、九节）。

    **永远返回一段文本，绝不返回空串或 None**——理由与 `format_style_recipe`
    完全相同：`render()` 不会因为缺键报错，调用方漏传这个参数，字面量
    `{{CONTEXT}}` 会原样进入发给模型的 prompt。

    ## 它与证据块是两块不同的东西

    证据块回答"这条判断有什么依据"；背景块回答"读者要能看懂，最少得知道什么"。
    两者混成一个块，模型立刻会把背景当成"又多了一条可以引用的事实"——
    而那就是工作单第四节要挡的。所以这里**不印事实编号**，也不写"依据"字样。

    ## `for_reader`

    Writer / Critic 看到的是"给读者进门用"的说明；Angle 阶段看到的是同一段背景
    加一句"不要把它写成判断"。同一份内容、不同的用途说明，比抄两份文本安全：
    抄两份必然漂，而漂了不会报错。
    """
    if context is None or not any(
        (getattr(context, name) or "").strip()
        for name in ("subject", "definition", "why_now", "core_tension")
    ):
        return (
            "（本次没有可用的话题背景：材料不足以支撑一段背景介绍。"
            "**不要为了补背景去编**——正文直接从证据能支撑的地方写起，"
            "并且不要让读者觉得你假设他已经知道了什么。）"
        )

    lines: list[str] = [
        "- 以下是**给不熟悉这个话题的读者**进门用的最小必要背景。"
        "它不是证据，不是结论，也不是本条的论点。",
    ]
    for field, title in (
        ("subject", "这个话题在讲什么"),
        ("definition", "需要解释的说法"),
        ("why_now", "为什么现在值得看"),
        ("core_tension", "里面真正的矛盾"),
    ):
        value = (getattr(context, field) or "").strip()
        if value:
            lines.append(f"- {title}：{value}")
    lines.append(
        "- 这段背景的来源："
        + context_origin_label(getattr(context, "origin", None))
        + "。"
    )
    if for_reader:
        lines.append(
            "- **读者不知道这个话题**。研究过程中你已经知道的东西，读者不知道——"
            "正文要让一个第一次听说它的人能读下去。"
        )
    else:
        lines.append(
            "- **这一段不是判断**：不要从背景里推出一个结论，也不要把它当成"
            "论证的依据——那是 `claim` 与 `evidence_ids` 的事。"
        )
    return "\n".join(lines)


def format_context_signals(
    context: Any | None, candidates: list[Candidate], pack: EvidencePack | None
) -> str:
    """逐条候选的**确定性**陌生读者信号，给 Critic 当参考事实（工作单第十四节）。

    与 `format_ai_signals` 同一条纪律：由程序算出、按需重算、不入库、不进 state，
    **只作为参考事实呈现，绝不混进 `context_fit` 的分数**。回传命中的具体对象是
    为了便于核对误报，不是自动判负。

    背景为空时返回一句"本次没有背景可参照"：那种运行里判"缺背景"只会是噪声，
    而噪声会让真是问题的那一条失去意义。
    """
    from app.agent.context import context_signals as _signals

    if context is None or not getattr(context, "subject", ""):
        return "（本次没有话题背景可参照，这一组信号本次不适用。）"

    signals = _signals(context, candidates, pack)
    if not signals:
        return "（本次没有话题背景可参照，这一组信号本次不适用。）"

    lines: list[str] = []
    for signal in signals:
        found: list[str] = []
        if signal.concept_jump:
            detail = "、".join(signal.entry_specifics) or "一个具体对象"
            found.append(f"首句直接抛出背景里没解释过的「{detail}」")
        if signal.evidence_hijacking:
            found.append(
                "首句在复述一条事实"
                + (f"（{signal.hijacked_fact_id}）" if signal.hijacked_fact_id else "")
                + "，而话题本身还没交代"
            )
        if signal.context_missing:
            found.append("全文与背景没有任何共同表述，没有接住这段背景")
        lines.append(
            f"[{signal.candidate_id}] "
            + ("；".join(found) if found else "未发现")
        )
    lines.append(
        "（这些是**可以复算的痕迹**，不是分数：命中了要自己看正文确认，"
        "没命中不代表正文一定好读。）"
    )
    return "\n".join(lines)


def format_evidence(pack: EvidencePack) -> str:
    """证据包列表（f1、f2…）。Angle 按 fact_id 引用它们。

    头一行先报**事实条数**与它们实际覆盖的**材料条数**（工作单第五节）：
    6 条事实可能全部来自同 1 条帖子，而"6 条证据"这句话本身在暗示有 6 份
    独立材料托着同一个判断。两个数印在一起，才读得出来这批事实是不是
    同一个来源拆出来的。

    作者数不在这里报——那要拿到本次材料才算得出（作者名在 research result
    上，不在证据包里），由 `extract_evidence` 的 summary 报（见
    tools/evidence.py，`viewpoint.authors_of`）。
    """
    if not pack.facts:
        return "（无证据）"
    materials = {sid for fact in pack.facts for sid in fact.source_ids}
    lines = [f"（共 {len(pack.facts)} 条事实，来自 {len(materials)} 条材料）"]
    lines += [
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


def format_candidates(candidates: list[Candidate], *, max_length: int | None = None) -> str:
    """候选清单。给了 `max_length` 就逐条附上**由程序算出**的字数。

    字数必须是算出来的，不是让模型估的：中文的字符数模型目测不出来（真机上
    `max_length=140` 写出过 188 字的稿子），而"比较两个给定的数"它做得可靠。
    Critic 判「超上限」正是这样一个比较——把数算好递给它，这一条才判得动。

    `max_length=None` 时输出与加这个参数之前**逐字相同**：这个函数目前只有
    Critic 一个调用方，但保持默认路径不变，才不会波及盯住这段渲染的测试。
    """
    if not candidates:
        return "（无候选）"

    def header(candidate: Candidate) -> str:
        head = f"[{candidate.candidate_id}]（{candidate.label}，第 {candidate.revision} 稿"
        if max_length is None:
            return f"{head}）"
        length = len(candidate.content or "")
        note = f"，{length} 字"
        if length > max_length:
            note += f"，**超出上限 {length - max_length} 字**"
        return f"{head}{note}）"

    return "\n\n".join(f"{header(c)}\n{c.content}" for c in candidates)


def format_length_headroom(content: str, max_length: int) -> str:
    """改稿前把"现在多少字、要砍多少"算给模型看。

    比只给一个绝对上限有用得多：模型数不准中文字数，但"把 188 砍掉 48"是一个
    它做得到的任务。数字一律由这里算，不交给模型估。

    未超长时的那一句是**防倒退**用的：不写，模型会顺手把一条已经合规的稿子
    也压缩一遍，于是改稿这一步反而丢掉了事实。
    """
    length = len(content or "")
    over = length - max_length
    if over <= 0:
        return (
            f"原稿 {length} 字，已在 {max_length} 字以内。"
            "**不要为了长度删内容**——这次改稿要解决的是别的意见，"
            "把篇幅改短不是改进，丢掉事实是退步。"
        )
    percent = round(over / length * 100) if length else 0
    return (
        f"**原稿 {length} 字，超过上限 {max_length} 字 {over} 字（约 {percent}%）。"
        f"这次改稿的第一件事就是砍掉至少 {over} 字——这是交付硬上限，不是建议。**\n"
        "砍的顺序：先合并重复的铺垫和转折，再删掉可有可无的修饰与形容词，"
        "最后才动句子结构。**不许删事实、不许删观点、不许改掉开头那个钩子**"
        "——把事实删了换来的合格长度，比超长更糟。\n"
        "**也不许把一句话砍成半句**：删的是整句与冗余修饰，不是句子的后半截。"
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

    相关性判定也印（`goal_relevance`）：它是"这一批候选是不是你要的东西"的
    唯一确定性结论（`app/agent/relevance.py`），而这两件事的处置完全相反——
    不达人可以换检索词再找，不相关则说明这一批材料本身就选错了。只印分数与
    分档的话，Planner 会看到一堆"弱支撑"的候选，然后合理地判断"再研究研究"，
    而正确的动作是"换一个词重新发现"。
    """
    if not topics:
        return "（尚未发现）"
    return "；".join(
        f"{t.topic_id} {t.title}（{t.trend_score:.1f} 分/{t.qualification.value}，"
        f"{t.author_count} 位作者，{_relevance_label(t)}）"
        for t in topics
    )


def _relevance_label(topic: Any) -> str:
    """候选与目标的相关性，三态各自的说法。

    `None`（本次没判定）写"相关性未判定"而不是"相关"：两者的含义不同，
    而 Planner 的下一步动作也不同——没判定时不该因为"这一栏写着相关"就放心。
    """
    relevance = getattr(topic, "goal_relevance", None)
    if relevance is None:
        return "相关性未判定"
    return "与目标相关" if relevance else "**与目标无关**"


def _angles(angles: list[Any], selected: Any | None) -> str:
    """把观点逐个印出来，带上它的证据结构与缺口。

    Phase 5B.1/5B.2 的调度能做得好，靠的是 Planner 的 `reason` 是**缺口驱动**的
    （"当前最高分观点只有 0 条反方证据"），而不是"继续研究"。只印一个
    "已生成 3 个"的名称列表，Planner 就无从知道缺什么——工作单第二十三节
    要求研究的方向有理由，而理由只能来自它看得见的东西。

    分数在这里印，理由是：这一步问的正是"选哪一个 / 要不要再研究"，
    分数是那个决定的依据。它与 `format_viewpoint` 的"不印分数"不矛盾——
    那边问的是"怎么写"。
    """
    selected_id = getattr(selected, "angle_id", None)
    ordered = sorted(
        angles, key=lambda a: -(getattr(a, "viewpoint_score", None) or 0.0)
    )
    scores = [getattr(a, "viewpoint_score", None) for a in ordered]
    ambiguous = (
        len(scores) > 1
        and scores[0] is not None
        and scores[1] is not None
        and (scores[0] - scores[1]) <= TIE_MARGIN
    )

    lines: list[str] = []
    for item in ordered:
        score = getattr(item, "viewpoint_score", None)
        assessment = getattr(item, "assessment", None)
        stance = getattr(item, "stance", None)
        mark = "（当前选定）" if item.angle_id == selected_id else ""
        if score is None or assessment is None:
            lines.append(f"  - {item.angle_id}{mark}：{item.claim}（未评估）")
            continue
        lines.append(
            f"  - {item.angle_id}{mark}：{score:.1f} 分，"
            f"{getattr(stance, 'value', '未定')}，"
            f"证据 {assessment.bound_facts}/{assessment.total_facts} 条、"
            f"来源 {assessment.bound_sources} 条、"
            f"反方 {assessment.bound_counterpoints} 条已绑定：{item.claim}"
        )
    if ambiguous:
        lines.append(
            f"  - 注意：前两名分差不超过 {TIE_MARGIN:g} 分（并列）。"
            f"若检索预算还有，优先补足那个观点缺的反方证据，而不是硬选一个。"
        )
    return "\n".join(lines)


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
        f"- 观点：已生成 {len(angles)} 个"
        + (f"，当前选定 {angle.angle_id}：{angle.claim}" if angle is not None else "，尚未选定")
        + (
            ""
            if not angles
            else "\n" + _angles(angles, angle)
        ),
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
