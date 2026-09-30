"""结构分类法：一篇文章"怎么组织"的有限集合（Phase 5B.2 工作单第十五节）。

## 这是假设，不是真理

分类法是**待验证的假设**，最终必须根据真实样本收敛。所以：

- 初始值来自工作单给的候选，共 10 类，**不为凑数硬造**；
- 允许 `unclassified`：一条样本归不进任何一类时，它就该归不进去。
  强行塞进最接近的一类是**污染**——Profile 的 `structure_distribution`
  会因此带上一个凭空出现的类别，而它看起来和别的类别一样可信。
- 改分类法要进 `TAXONOMY_VERSION`。两个版本的分布不可直接比较。

## 一个结构是**类别**，不是写作模板

工作单第十六节的关键区分：Writer 从 Profile 里**选一个结构**，而不是往模板里
填空。所以这里的每个成员描述的是一篇文章的组织方式，不是"第一段写什么、
第二段写什么"。后者会把风格蒸馏退化成填空题——那正是这一阶段要摆脱的东西。

## 与 `prompts/generate.txt` 的关系

V1 的 `prompts/generate.txt:15` 已经有一套 `hook_type` 词汇
（question / statistic / statement / list / story），但那套值算出来就被丢掉了
（`app/agent/nodes/generate.py` 只用 `draft.content`），从不落库、从不上界面。
本模块的 `HookType` **显式取代**它，并保留原来那五个值，另外补两个。
不并存两套钩子词汇——那会让"这个 hook 到底属于哪套"变成又一个没有答案的问题。
"""

from __future__ import annotations

from enum import Enum

#: 分类法版本。改成员、改定义、改归属规则都要进位。
#: `StyleProfile` 上存这个值：两个版本算出的分布不可直接比较。
TAXONOMY_VERSION = "1.0"


class StructureType(str, Enum):
    """一篇文章的组织方式。

    成员之间应当**互斥**：一条样本最好只有一个归属。如果实践中大量样本
    同时符合两类，说明分类法切错了维度——那是要修分类法的信号，
    不是要允许一个样本挂两个标签的信号。
    """

    #: 先给判断，再给理由。最短、最"像一个人在说话"的一种。
    OPINION_FIRST = "opinion_first"
    #: 先摆事实/数据，再引出判断。与 OPINION_FIRST 的差别只在顺序。
    FACT_FIRST = "fact_first"
    #: 提出一个问题，然后回答它。
    QUESTION_ANSWER = "question_answer"
    #: A 与 B 的对比，落在一个判断上。
    CONTRAST = "contrast"
    #: 步骤/清单式，教怎么做。
    HOW_TO = "how_to"
    #: 一个具体案例的复盘。
    CASE_REVIEW = "case_review"
    #: 先立一个常见看法，再反驳它。
    COUNTERINTUITIVE = "counterintuitive"
    #: 描述现象/趋势，少下结论。
    OBSERVATION = "observation"
    #: 围绕一组数字展开。
    DATA_READING = "data_reading"
    #: 基于现状推演未来。
    PREDICTION = "prediction"
    #: 归不进以上任何一类。**必须保留**：允许归不进去，
    #: 才谈得上"分类法根据真实样本收敛"。
    UNCLASSIFIED = "unclassified"


class HookType(str, Enum):
    """开头用什么抓住读者。取代 V1 那套被丢掉的词汇（见模块 docstring）。"""

    QUESTION = "question"
    STATISTIC = "statistic"
    STATEMENT = "statement"
    LIST = "list"
    STORY = "story"
    #: 新增：用一个反差/意外起头。
    CONTRAST = "contrast"
    #: 新增：引一句原话或一个具体指称起头。
    QUOTE = "quote"
    UNCLASSIFIED = "unclassified"


class ContentType(str, Enum):
    """这条帖子大体上是什么性质的内容。

    与 `StructureType` 正交：一条"观点文"可以写成"对比"结构，
    也可以写成"问答"结构。合成一个维度会让两者都无法回答。
    """

    OPINION = "opinion"
    ANALYSIS = "analysis"
    NEWS = "news"
    TUTORIAL = "tutorial"
    EXPERIENCE = "experience"
    ANNOUNCEMENT = "announcement"
    UNCLASSIFIED = "unclassified"


#: 账号分类（工作单第七节给定的枚举）。存成字符串而不是数据库枚举：
#: 分类会随认知调整，改一个常量比改一次数据库类型容易。
ACCOUNT_CATEGORIES: tuple[str, ...] = (
    "AI",
    "大模型",
    "开发者",
    "芯片",
    "科技媒体",
    "创业",
    "产品",
)


#: 语料的取样窗口，**刻意与 trend 的 72 小时窗口分开**（工作单决策 A）。
#: 72 小时回答的是"为什么这条讨论是**现在**值得写的"；7 天回答的是
#: "这些人**平时**怎么写"。两个口径混在一起会让两边都失去意义。
#: 取 6 天而不是正好 7 天：`end_time` 有最小滞后钳制，从 7 天整往回数会
#: 丢掉窗口最老的那几秒帖子。
CORPUS_WINDOW_DAYS = 6


def coerce_label(enum_cls: type[Enum], value: object, *, default: str = "unclassified") -> str:
    """把 LLM 给的标签收进枚举；收不下就落到 `unclassified`。

    **不抛异常。** 模型偶尔返回近义写法（"opinion" / "观点直给"），
    为此让整条标注失败，代价远大于把它归成 unclassified——后者会如实地
    在分布里显示成"这一类没归进去"，前者会让整批样本少一条而没人知道。

    也**不做模糊匹配**：把"看起来像"的标签硬映射到某个成员，等于让分类法
    的收敛被一个字符串距离算法悄悄决定。宁可 unclassified。
    """
    if isinstance(value, enum_cls):
        return value.value
    text = str(value or "").strip().lower()
    for member in enum_cls:
        if text == member.value:
            return member.value
    return default
