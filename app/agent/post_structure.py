"""中文 X 帖子的**传播结构**写作策略。

## 它与 StyleRecipe 的分工

- `StyleRecipe`（`app/style/`）说「**这批语料平时长什么样**」：结构类型、长度、
  密度、语气——每一项都是从被追踪账号的样本里**统计**出来的。
- 这一层说「**一条值得被讨论的帖子该怎么组织**」：开头用什么形式、一条帖子只留
  几个判断、信息怎么支撑、节奏、怎么收尾。它是**产品级的写作契约**，
  不依赖语料，也不从任何账号身上学。

两者互补而不是二选一：recipe 让稿子像这个账号平时写的东西，这一层让稿子
值得被读完。冲突时以这一层为准——形状可以让步，可读性不让。

## 为什么不并进 StyleRecipe

`build_recipe` 在 corpus 为空时返回一个**中性 recipe 并提前返回**。把写作原则
塞进 `StyleRecipe` 会让它们恰好在「还没采到语料」的运行里消失——而那正是最需要
它们的运行。写作原则与语料无关，就不该挂在语料才有的那个对象上。

同理**不改** `StyleRecipe` 的字段、语义与版本哈希：这一层没有改变「这次运行
采用什么写作结构」这一个决策，只是把与语料无关的写作原则单独说出来。

## 为什么是代码而不是又一段提示词散文

这里只放**必须精确、且能判定**的东西：

- 三条候选的**角色**（名字与要求在一处定义，prompt、测试、离线用例读同一份）；
- **证据形状**派生出的可执行指令（本次到底有没有可核验的数字、事实来自几条
  材料）——这一条只能由程序算：让模型自己判断「我手上有没有数字」，
  它会在没有数字的时候照样写出一个「约 30%」。

其余写作要求（禁用开头、套话清单、节奏）是散文，留在
`prompts/agent/candidates.txt` 里，由测试钉住。

## 版本

`POST_STRUCTURE_VERSION` 覆盖的是**上面这些结构化契约**：角色集合、开头/结尾
形式集合、证据形状指令的判定规则。改动其中任何一项都要进位——它与
`app/style/schemas.py::FEATURE_VERSION`、`evals/__init__.py::__version__` 是
同一条纪律：口径变了而版本没动，两份报告会被读成「稿子变了」。

## 长度：偏好与硬约束是两回事

`max_length`（`persona.max_length`，140）是**硬上限**：最终交付、可审核、
可发布的正文一条都不许超过它。`recommended_min_length`（60）只是**写作偏好**，
短于它不构成任何拒绝理由。

这两个数是不同类的东西，所以在这一层只有一个 `over_hard_limit()` 负责判界，
调用方（Critic 的确定性拦截、finalize 的选稿、发布预检）读的都是它。
"超长"因此不是靠提示词里的措辞成立的——措辞只降低概率，判界由代码说了算。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.agent import viewpoint
from app.style.recipe import evidence_signals

#: 结构化写作契约的版本。改动角色/开头/结尾形式，或证据形状指令的判定规则，
#: 都要进位。散文部分由 `tests/test_agent_post_structure.py` 钉住。
#:
#: 1.1：证据形状里那条"没有数字"的指令**换了判据**——从"不许出现数字字符"
#: 收窄到"不许出现外部事实性的数字断言"（结构性编号、材料给定数值的推算不在
#: 其内），并加入长度硬上限的判界函数。指令变了而版本不动，两份报告会被读成
#: "同一套规则下稿子变了"。
POST_STRUCTURE_VERSION = "1.1"


class OpeningMove(str, Enum):
    """一条帖子的第一句用什么方式把人拉住。

    与 `app/style/taxonomy.py::HookType` 的关系：`HookType` 是**语料统计**出来的
    开头分类（这批人平时怎么写），这里是**本次生成**可以选的开头动作
    （这一条该怎么写）。两者名字相近但用途不同，所以没有合并——
    合并会让「语料里统计不到的开头形式」从选项里消失。

    `EXPERIENCE` 带一个硬前提：只有材料或目标真的给了那段经历才能用
    （见 `render_evidence_shape` 的第一人称红线）。
    """

    VERDICT = "verdict"
    FACT = "fact"
    CONTRAST = "contrast"
    QUESTION = "question"
    EXPERIENCE = "experience"
    SCENE = "scene"


class EndingMove(str, Enum):
    """一条帖子怎么收尾。**不要求每条都用问句。**"""

    CONCLUSION = "conclusion"
    QUESTION = "question"
    CHOICE = "choice"
    TENSION = "tension"
    CONCRETE = "concrete"


class CandidateRole(str, Enum):
    """三条候选各自的**表达路径**。

    三条必须在组织方式上真的不同。这和「生成三条」是两件事：三条同义改写
    等于只写了一条，而 Critic 面对三条同质候选也无从比较——它挑不出
    「哪条更适合这个角度」，只能挑「哪条措辞更顺」。
    """

    VERDICT = "verdict"
    FACT_CONTRAST = "fact_contrast"
    DISCUSSION = "discussion"


#: 角色 → 中文名。写在候选 prompt 里，也进 Critic 的候选表头（`Candidate.label`），
#: 于是「三条候选走的是哪三条路」在 prompt、评审、控制台三处是同一个词。
ROLE_LABELS: dict[CandidateRole, str] = {
    CandidateRole.VERDICT: "鲜明观点型",
    CandidateRole.FACT_CONTRAST: "事实与反差型",
    CandidateRole.DISCUSSION: "讨论与不同视角型",
}

#: 角色声明顺序。候选按这个顺序生成，`c1`/`c2`/`c3` 与这张表一一对应。
ROLE_ORDER: tuple[CandidateRole, ...] = (
    CandidateRole.VERDICT,
    CandidateRole.FACT_CONTRAST,
    CandidateRole.DISCUSSION,
)

_OPENING_MOVE_INSTRUCTIONS: dict[OpeningMove, str] = {
    OpeningMove.VERDICT: "第一句直接说出你的判断。适合你手上证据够、逻辑清楚的时候。",
    OpeningMove.FACT: "第一句给出最值得注意的那**一个**数据或变化。只给一个。",
    OpeningMove.CONTRAST: "第一句把两个事实之间的落差摆出来，让读者先觉得不对劲。",
    OpeningMove.QUESTION: "第一句提出一个具体、真的值得回答的问题（不是修辞性的空问）。",
    OpeningMove.EXPERIENCE: "从一段真实经历里的发现切入——**只有材料或目标真的给了这段经历才能用**。",
    OpeningMove.SCENE: "从一个日常情境切入，适合生活化的选题。不要为了亲切硬造场景。",
}

_ENDING_MOVE_INSTRUCTIONS: dict[EndingMove, str] = {
    EndingMove.CONCLUSION: "把判断说完就停。观点已经完整时这是最好的结尾。",
    EndingMove.QUESTION: "问一个读者答得出来、也愿意答的问题。**不要求每条都这样收尾。**",
    EndingMove.CHOICE: "给出一个具体的选择（二选一、A/B/C），让读者能表态。",
    EndingMove.TENSION: "把一个还值得讨论的矛盾留在那里，不急着替读者下结论。",
    EndingMove.CONCRETE: "用一个具体、有力的短句结束，不要用一句谁都写得出的总结。",
}


def render_opening_moves() -> str:
    """开头形式的清单。

    **渲染成清单而不是形容词**：模型需要的是「这一条可以怎么开头」这个可选集合，
    不是一个「要有钩子」的抽象要求。集合里同时给出适用条件，是因为开头形式
    服务于选题——生活化的选题用「鲜明判断」开头会显得假。
    """
    lines = ["本次可以用的开头形式（**三条候选必须至少用两种不同的形式**）："]
    lines += [f"- {move.value}｜{text}" for move, text in _OPENING_MOVE_INSTRUCTIONS.items()]
    lines.append(
        "- **不许**三条件都用同一种开头。也不许用这些套话起头："
        "「一个扎心的事实」「冷知识」「我发现」「真相令人震惊」「背后水很深」"
        "「事情没那么简单」「我必须说句实话」「99% 的人都不知道」。"
    )
    lines.append(
        "- 不许为了留住读者而**隐瞒关键事实**：钩子是把最重要那件事说在前面，"
        "不是把答案藏起来。也不许让标题式的那一句夸大事实，正文却托不住它。"
    )
    return "\n".join(lines)


def render_ending_moves() -> str:
    """结尾形式的清单。

    「不要求每条都以问句结尾」这一句必须显式写出来。不写的话，模型会把
    上一节「要有互动」读成「每条结尾都加一个你怎么看」——而乞求互动恰好是
    这一节要挡住的。
    """
    lines = ["结尾形式（按内容选，**不要求每条都提问**）："]
    lines += [f"- {move.value}｜{text}" for move, text in _ENDING_MOVE_INSTRUCTIONS.items()]
    lines.append(
        "- **不要为了互动而乞求互动**：不写「你怎么看」「评论区见」「欢迎讨论」"
        "「点赞关注」。互动意愿来自内容里有分歧、有信息、有读者能接上的话头，"
        "不是来自一句邀请。观点已经完整时，自然结束就是最好的结尾。"
    )
    return "\n".join(lines)


def render_candidate_roles() -> str:
    """三条候选的三条路。

    写成「角色 + 适用 + 要求 + 禁忌」是因为只给一个角色名，模型会三条都写成
    同一个声音、只换第一句。每条都带**禁忌**，是为了挡住最常见的退化方式：
    第 2 条堆数字、第 3 条给第 1 条加个问号。
    """
    return """三条候选走三条不同的路，**必须真的不同**——三条同义改写等于只写了一条：

1. **鲜明观点型**：第一段就是你的判断，不铺垫。
   适用：证据够、逻辑清楚、有明确行业观察或真实经验的选题。
   要求：至少给一个有意义的理由或一个具体观察，不能只有一句口号。
   禁忌：为了显得有态度，把结论说得比证据更笃定。

2. **事实与反差型**：用一个事实、一个数字、一个对比或一个具体案例形成记忆点。
   要求：优先用本次证据里**最有解释力**的那一条；把数字和判断之间的关系讲出来
   ——数字自己不会说话，读者记住的是那个反差。
   禁忌：只罗列数字；为了冲击力做一条错误的比较；**没有可靠数字时不许编一个**
   （改用具体案例、机制解释或另一个有依据的说法）。

3. **讨论与不同视角型**：提出一个具体、值得回答的问题，或从一个不同于常见说法的角度切入。
   要求：问题足够具体，读者能轻松说出自己的看法；可以用选择、反直觉的判断或明确的分歧。
   禁忌：为了评论量故意造一个假的二选一；或者只是把第 1 条的核心表达加一个问号。

**不是三个模板**：选题不适合某一条路时，允许按实际内容调整，但三条之间的
组织方式仍必须不同——差别要大到读者读完三条件，记住的是三件不同的事。"""


# ---------------------------------------------------------------------------
# 数字：编造的是**断言**，不是数字字符
# ---------------------------------------------------------------------------

#: 结构性数字的例外条款。**必须与"没有数字"那条同时出现**。
#:
#: 上一版把这条规则写成了"不许出现任何数字"，代价是误杀「三个问题」「二选一」
#: 「第一种做法」这类表达——它们在数的是正文里刚说过的东西，不是在断言外部世界
#: 存在某个量，读者也不会把它读成一个事实。写成自由文本而不是一个"结构性数字"
#: 常量表，是因为这件事的判据是**语义**（这个数字在数什么），不是词形：
#: 一张词表既拦不住编造，又会把合法的写法误判成违规。
_STRUCTURAL_NUMBER_RULE = (
    "- **「没有数字」不等于「不许出现数字字符」。** 给选项编号（「三个值得注意的问题」"
    "「二选一」「第一种做法」）、说清「我自己写了三条候选」这类**结构性表达**不受限制："
    "它们数的是正文里已经交代过的东西，不构成对外部世界的量化断言。"
)

#: 材料给了数字时，允许的推算。与"没有数字"那条互斥——两条永远不会同时出现。
_COMPUTED_NUMBER_RULE = (
    "- 材料给了数值时，**口径一致、算法能追到输入值**的推算可以写：同比、倍数、"
    "差额、把两个已知量加起来。判据是「这个数能不能从材料里的数一步步算回来」，"
    "**不是**「它在来源里有没有原样出现过」——所以不要因为一个算出来的数没被原样"
    "引用就把它当成编造。反过来说，算出来的数也只能当**推算**用：不许把它说成"
    "来源里的原始统计，也不许在口径不同的两个数之间做比较。"
)


@dataclass(frozen=True)
class EvidenceShape:
    """本次证据的形状：能数的部分。

    全部来自程序统计，不由模型判断。`has_numeric_fact` 尤其如此——让模型自己
    判断「我手上有没有数字」，它在没有数字的时候照样会写出一个「约 30%」。
    """

    fact_count: int = 0
    material_count: int = 0
    counterpoint_count: int = 0
    unknown_count: int = 0
    has_numeric_fact: bool = False
    #: 本次材料涉及多少位作者。`None` = 调用方没给（渲染时按「未统计」处理，
    #: **不按 0 处理**——0 位作者是一句关于语料的结论，而这里是没算过）。
    author_count: int | None = None

    @property
    def has_facts(self) -> bool:
        return self.fact_count > 0


def evidence_author_count(state) -> int:
    """这次证据实际由多少位作者支撑。

    与 `app/agent/tools/evidence.py` 报「独立支撑」时算的是同一件事，用的是同一个
    索引（`viewpoint.evidence_index` + `authors_of`）。**不复用另一个来源**：
    `research_results` 里还带着本轮**没有被引用**的材料，把它们的作者算进来，
    「3 位作者」就会变成一句比证据本身更宽的话。

    拿不到作者名的来源不进集合（同 `authors_of` 的理由）。返回 0 是有意义的：
    它说明这份证据没有一位可核实的作者，渲染成「作者 0 位」。

    放在这里而不是各工具里：Writer 与 Critic 必须看到**同一个**形状。
    两边算出不同的数字时，Critic 会拿「没有数字」去判一条被要求用数字写的正文，
    而那不会报错，只会让评审结论莫名其妙。
    """
    pack = state.get("evidence_pack")
    if pack is None:
        return 0
    index = viewpoint.evidence_index(
        pack,
        state.get("research_item_ids") or [],
        state.get("research_results") or [],
    )
    authors, _missing = viewpoint.authors_of(
        viewpoint.bound_sources_of(
            [getattr(fact, "fact_id", "") for fact in (getattr(pack, "facts", None) or [])],
            index,
        ),
        index,
    )
    return len(authors)


def evidence_shape(pack: object | None, *, author_count: int | None = None) -> EvidenceShape:
    """从 EvidencePack 抽形状。`None` 返回全零。

    `fact_count` / `counterpoint_count` / `has_numeric_fact` 直接复用
    `app/style/recipe.py::evidence_signals`：那里的数字检测刻意只看**事实正文**，
    绝不 `str(fact)` 兜底——`EvidenceFact` 的 repr 里带着 `fact_id='f1'`，
    那个 `1` 会让「有数字」对每一条事实都为真。自己再写一遍这段判断，
    迟早会把那个坑重新踩一次。
    """
    if pack is None:
        return EvidenceShape(author_count=author_count)

    signals = evidence_signals(pack)
    facts = getattr(pack, "facts", None) or []
    materials = {sid for fact in facts for sid in (getattr(fact, "source_ids", None) or [])}

    return EvidenceShape(
        fact_count=signals.fact_count,
        material_count=len(materials),
        counterpoint_count=signals.counterpoint_count,
        unknown_count=signals.unknown_count,
        has_numeric_fact=signals.has_numeric_fact,
        author_count=author_count,
    )


def over_hard_limit(content: str, max_length: int) -> int:
    """正文超过**硬上限**多少字符。没超（含恰好等于上限）返回 0。

    这是全仓唯一一处判"超长"的地方：Critic 的确定性拦截、finalize 的选稿、
    发布预检、以及递进 prompt 的差值，读的都是它。口径与
    `guardrail.evaluate` 的 `too_long` 一致——`len(content) > max_length`，
    恰好等于上限**不算超**。

    写成函数而不是各处现算，是因为这几处只要漂一位，表现就是
    "评审说长度没问题、交付那一步却交不出去"，而那条回路不报错，只是空转。
    """
    return max(0, len(content or "") - max_length)


def length_detail(content: str, max_length: int) -> str:
    """一句话说清一条正文的长度状况，数字全部由这里算。

    给人看的（评审摘要、失败信息）与给模型看的（改稿指令）都从这一句出发，
    于是"超了多少字"在两处永远是同一个数。
    """
    length = len(content or "")
    over = over_hard_limit(content, max_length)
    if over == 0:
        return f"正文 {length} 字，在硬上限 {max_length} 字以内"
    return f"正文 {length} 字，超过硬上限 {max_length} 字 {over} 字"


def render_evidence_shape(shape: EvidenceShape) -> str:
    """证据形状 → 一组可执行的写作指令。

    这是这一层里**唯一**会让模型一写错就编数字的地方，所以它必须把两种情形
    分开说死：有数字时怎么用，没数字时**明确禁止**出现数字。只写「使用具体
    数据」会让没有材料的那一轮照样长出一个百分比。
    """
    if not shape.has_facts:
        return (
            "（本次**没有任何可用事实**：不要写具体数字、金额、比例或案例，"
            "也不要用第一人称经历来补——那是编。要么把判断写成谨慎的观察，"
            "要么让这次运行继续去研究材料。）"
            + _STRUCTURAL_NUMBER_RULE
        )

    authors = "未统计" if shape.author_count is None else f"{shape.author_count} 位"
    lines = [
        f"本次证据的形状（**由程序统计，直接照它写**）：可用事实 {shape.fact_count} 条，"
        f"来自 {shape.material_count} 条材料；反方 {shape.counterpoint_count} 条；"
        f"作者 {authors}。",
    ]

    if shape.has_numeric_fact:
        lines.append(
            "- 材料里**有**可核验的数字：事实型那条要优先用它们，并把数字与判断之间的关系"
            "讲清楚。用数字是为了让读者记住那个反差，不是为了显得专业——"
            "**只挑与判断直接相关的那一个**，不要堆一串。"
        )
        lines.append(
            "- 数字必须保留单位、时间范围和统计口径。**不许**把相关写成因果，"
            "**不许**把「可能」写成「已经确定」。"
        )
        lines.append(_COMPUTED_NUMBER_RULE)
    else:
        lines.append(
            "- 本次材料里**没有**可核验的数字——注意它管的是**外部世界的事实性量化"
            "断言**：股价、收入、人数、百分比、增长率、日期、统计排名。"
            "这一类三条件都不许出现具体的数字、比例、金额、倍数或排名，"
            "不许凭印象写一个「大概三成」。"
            "需要具体感时改用：一个具体案例、一个机制解释，或者一个有依据的比较。"
            "**宁可只讲道理，也不要编一个数字。**"
        )
        lines.append(_STRUCTURAL_NUMBER_RULE)

    if shape.material_count == 1:
        lines.append(
            "- 全部事实来自**同 1 条材料**：不要写成「业内普遍认为」「大家都在说」这类共识口径，"
            "也不要把它说成一个已经被多方确认的结论。"
        )

    lines.append(
        "- 系统**没有**给你任何关于你自己的材料（材料都是别人的公开帖子）。"
        "除非本次目标里明确写了某段经历并给了结果，否则三条件都不许出现第一人称的"
        "经历、业绩、持仓、实测结果或「我亲眼见到」——"
        "**编造个人经历和编造数字是同一种错误**，而且更难被读者发现。"
    )
    return "\n".join(lines)


__all__ = [
    "POST_STRUCTURE_VERSION",
    "ROLE_LABELS",
    "ROLE_ORDER",
    "CandidateRole",
    "EndingMove",
    "EvidenceShape",
    "OpeningMove",
    "evidence_author_count",
    "evidence_shape",
    "length_detail",
    "over_hard_limit",
    "render_candidate_roles",
    "render_ending_moves",
    "render_evidence_shape",
    "render_opening_moves",
]
