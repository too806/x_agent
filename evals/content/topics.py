"""固定中文选题 + 参考评价标准（**离线**，不联网、不调用 LLM）。

## 这是评价样例，不是写作模板

这一份东西回答的是"一条帖子该怎么被**评**"，不是"一条帖子该怎么**写**"。
它**不会**进入生产 Writer 的提示词——`app/` 一行都不 import 它（评估层只读
生产代码，依赖是单向的）。生产那边拿到的是 `app/agent/post_structure.py` 里
那份**与语料无关的写作契约**；这一边是给它配的一份**人工可读的评判样例**：
六个中文选题类别，每个类别配上"这一类里最容易失手的地方"和八条通用判据。

## 为什么要有它

八条判据（工作单第六节）本身与选题无关，但"哪一条最容易在这一类选题上出事"
是随选题变的：

- 「AI Agent 开发经历」这一类，模型最容易**给自己编一段经历**——而系统没有
  给作者任何关于他自己的材料；
- 「数字资产」「美股与投资」这一类，模型最容易**编一个涨跌幅**，或者把一个
  统计上的相关写成因果；
- 「社会经济讨论」这一类，模型最容易写成一堆正确但没人反对的空话。

把这三件事分开写，是因为它们在 Critic 那里落到的位置不同：第一类是 P0
（编造），第二类是 P0 或 P1（证据越界 / 动机当事实），第三类是 P1（立场不清）。
合成一句"要具体、要有依据"，三种失手就都看不出来了。

## 判据的写法

每条判据是 `(判定问题, 什么算不合格)` 两段。第二段必须**可判定**——
"不合格"写成"写得不好"没有意义，写成"读完说不出作者认为什么"才用得上。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.post_structure import CandidateRole, OpeningMove


@dataclass(frozen=True)
class Criterion:
    """一条参考判据：问什么、什么算不合格。"""

    question: str
    fail_signal: str


#: 工作单第六节的八项检查。**通用**，与选题无关——所以只写一遍。
#: `critic.txt` 里是给模型的版本，这里是给人/离线评估读的版本，两边的检查项
#: 必须一一对应（`test_the_offline_criteria_match_the_critic_checks` 钉住这一点）。
CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        question="开头有效性：读完第一句，有没有理由读第二句？",
        fail_signal="第一句是通稿式开头，或者用套话（「一个扎心的事实」「我发现」）起头。",
    ),
    Criterion(
        question="观点清晰度：读完能不能复述出作者认为什么？",
        fail_signal="读完只能复述出一堆事实，说不出作者的判断。",
    ),
    Criterion(
        question="具体程度：说的是这一件事，还是放之四海皆准的话？",
        fail_signal="整篇换一个选题照样成立（「AI 时代已经到来」这类）。",
    ),
    Criterion(
        question="表达自然度：像不像一个人在说话？",
        fail_signal="AI 腔、术语堆成墙、或者把一句完整的话拆成碎片凑节奏。",
    ),
    Criterion(
        question="结构差异性：三条候选是不是真的走了三条路？",
        fail_signal="三条互相换掉措辞之后没有剩下不同的东西（同义改写）。",
    ),
    Criterion(
        question="讨论潜力：读完会不会有人想补充经验或明确反对？",
        fail_signal="最多只让人点个赞——没有分歧、没有可接的话头。"
        "（注意：结尾有问号本身**不**构成这一项达标。）",
    ),
    Criterion(
        question="证据一致性：说得有没有超出证据能支撑的范围？",
        fail_signal="把推测写成事实、把相关写成因果、把动机写成内幕，或者凭空出现数字。",
    ),
    Criterion(
        question="原创性与完整性：这是不是一条能直接发出去的帖子？",
        fail_signal="正文里带着内部编号、版本号、「帖子如下」这类元说明，"
        "或者出现第一人称的经历与业绩。",
    ),
)


@dataclass(frozen=True)
class TopicCase:
    """一个固定选题类别，以及它特有的失手方式。"""

    case_id: str
    category: str
    goal: str
    #: 这一类选题最适合的两条路（`CandidateRole`）。三条候选里至少要有这些。
    fit_roles: tuple[CandidateRole, ...]
    #: 这一类选题适合的开头形式。
    fit_openings: tuple[OpeningMove, ...]
    #: 这一类里最容易失手的地方。**必须具体**，不能是"要写得更好"。
    risks: tuple[str, ...]


CASES: tuple[TopicCase, ...] = (
    TopicCase(
        case_id="T-AI-INDUSTRY",
        category="AI 行业判断",
        goal="说说最近这一轮模型能力提升对普通开发者的实际影响，给一个明确判断",
        fit_roles=(CandidateRole.VERDICT, CandidateRole.FACT_CONTRAST),
        fit_openings=(OpeningMove.VERDICT, OpeningMove.CONTRAST),
        risks=(
            "写成一串产品名的罗列：读完知道发布了很多东西，不知道作者认为哪一件事重要。",
            "把「会取代一部分工作」写成「会取代这个职业」——范围超出证据。",
            "用「AI 时代已经到来」这类空话开头或收尾。",
        ),
    ),
    TopicCase(
        case_id="T-US-EQUITY",
        category="美股与投资",
        goal="谈谈最近某类公司的估值变化说明了什么，不要给投资建议",
        fit_roles=(CandidateRole.FACT_CONTRAST, CandidateRole.DISCUSSION),
        fit_openings=(OpeningMove.FACT, OpeningMove.CONTRAST),
        risks=(
            "编一个涨跌幅或市盈率——材料里没有可核验的数字时，正文里一个数字都不许有。",
            "把「估值变化与某件事同时发生」说成「因为那件事」——相关不等于因果。",
            "读起来像投资建议（「应该买」「还会涨」）——观点要落在对行业的判断上，"
            "不是落在别人的钱上。",
        ),
    ),
    TopicCase(
        case_id="T-DIGITAL-ASSET",
        category="数字资产",
        goal="谈谈稳定币或链上结算最近的一个变化意味着什么",
        fit_roles=(CandidateRole.VERDICT, CandidateRole.DISCUSSION),
        fit_openings=(OpeningMove.VERDICT, OpeningMove.SCENE),
        risks=(
            "用阴谋论式的话给出张力（「背后水很深」「事情没那么简单」）——"
            "张力必须来自材料本身。",
            "把某一方的动机当成已证实的事实（「他们就是为了绕开监管」）。",
            "写出一条无来源的金额、倍数或排名来增加冲击力。",
        ),
    ),
    TopicCase(
        case_id="T-AGENT-DEV",
        category="AI Agent 开发经历",
        goal="说说做 Agent 产品这一段时间里，哪个环节最被低估",
        fit_roles=(CandidateRole.VERDICT, CandidateRole.FACT_CONTRAST),
        fit_openings=(OpeningMove.CONTRAST, OpeningMove.VERDICT),
        risks=(
            "**编造个人经历**：系统没有给作者任何关于他自己的材料，"
            "「我刚上线了一个 Agent」「我实测了三家模型」都是编造——"
            "除非本次目标里明确写了这段经历和结果。",
            "编一个实测数字（延迟、成功率、成本）来支撑观点。",
            "写成一份技术清单：读者读完知道做了哪几件事，不知道作者认为哪一件最难。",
        ),
    ),
    TopicCase(
        case_id="T-MAKING-MONEY",
        category="赚钱路径与创业",
        goal="谈谈这一轮 AI 工具把哪一类小生意的门槛降低了，给一个明确判断",
        fit_roles=(CandidateRole.VERDICT, CandidateRole.DISCUSSION),
        fit_openings=(OpeningMove.VERDICT, OpeningMove.QUESTION),
        risks=(
            "写「普通人一定要抓住机会」这类谁都不反对、谁也用不上的话。",
            "编一个收入数字或个人业绩（「我最近靠这个赚了」）——这是编造，不是生动。",
            "把一条个案说成一个普遍规律（「有人这么做了，所以这条路成立」）。",
        ),
    ),
    TopicCase(
        case_id="T-SOCIO-ECONOMIC",
        category="社会经济讨论",
        goal="谈谈某个行业的就业结构变化，把它放在更长的时间里看",
        fit_roles=(CandidateRole.DISCUSSION, CandidateRole.FACT_CONTRAST),
        fit_openings=(OpeningMove.CONTRAST, OpeningMove.QUESTION),
        risks=(
            "通篇正确但无人反对的空话（「未来充满不确定性」「认知决定命运」）。",
            "为一个复杂现象指定一个单一原因，并把推测写成结论。",
            "收尾用一句「你怎么看」把判断让回给读者——结尾不要求提问，也不许乞求互动。",
        ),
    ),
)


__all__ = ["CASES", "CRITERIA", "Criterion", "TopicCase"]
