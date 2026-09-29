"""AI 味检测（第一版：**只做确定性指标**）。

## 为什么第一版不接 LLM

"这段话像 AI 写的吗"这件事，当事人（模型）不是可靠的裁判：同一个模型写出来的
东西，让它自己评价，系统性地偏宽松，而且换个模型、换个 temperature 结论就变。
一个不可复现的评分进不了比较体系——而比较（Version A vs Version B）正是这个
Evaluation Lab 存在的理由。

所以这一版只做**能算准的**：

- 套话计数（固定词表，逐字匹配）
- 句长分布（均值、方差）
- 具体实体计数（数字、拉丁词、@handle、#hashtag、引号内的专名）
- 问句/叹句计数
- 重复片段计数
- 抽象名词密度（**近似**，见下）

以及一个**永远不由本模块填写**的字段：`human_ai_style_rating`。
它是真值，只能由人给。将来若引入 LLM 打分，它只能作为**辅助信号**
写进另一个字段，不能覆盖人工标注。

## 哪些指标是近似的

中文没有分词也能做的事情很多，但"抽象名词"不在此列：靠后缀（性/化/度/力/感）
匹配出来的东西里混着大量普通名词（"温度""力气""时代"）。所以这一项的名字里
带 `_approx`，并且出现在 `approximate_metrics` 里——报告会明确告诉读者
"这个数别单独用"。**能算但不准**和**算不出来**是两件事：前者照样报，
但要标出来；后者报 unavailable。见 `evals/values.py`。
"""

from __future__ import annotations

import re
import statistics
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from evals.values import UNAVAILABLE, MetricValue, rate

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

#: 套话。命中即计数，命中的词原样回传，方便人核对"这个也算套话？"。
#: 词表是**评估资产**，会随认知变化——但改它要同时进位
#: `evals.__version__`，否则两份报告会因为口径不同而被误读成"稿子变了"。
GENERIC_PHRASES: tuple[str, ...] = (
    # 中文：起承转合的连接件
    "在当今", "在如今", "随着", "在这个", "背景下", "大背景下",
    "综上所述", "总而言之", "总的来说", "由此可见", "不难看出",
    "值得注意的是", "需要指出的是", "不可否认", "毋庸置疑", "毫无疑问",
    "由此可见的是", "换句话说", "从某种意义上",
    # 中文：空转的形容词
    "颠覆性", "革命性", "前所未有的", "极大地", "深刻的变革",
    "引发了广泛关注", "具有重要意义", "深远的影响", "新的高度",
    "赋能", "闭环", "抓手", "颗粒度", "底层逻辑", "心智",
    "拉通", "对齐", "复盘", "组合拳", "护城河", "双刃剑",
    # 中文：面向读者的口号
    "让我们", "我们不妨", "各位", "朋友们",
    # 英文
    "in today's", "in the ever-evolving", "it's worth noting",
    "it is important to note", "game-changer", "game changer",
    "delve into", "the landscape of", "unlock the", "leverage the",
    "in conclusion", "at the end of the day", "paradigm shift",
)

#: 模糊限定词。**不是**套话，单独一栏：一个观点稿里出现一两次是诚实，
#: 出现十次是没观点。两者要分开看，所以不并进套话计数。
HEDGES: tuple[str, ...] = (
    "可能", "或许", "也许", "大概", "似乎", "看起来",
    "某种程度上", "一定程度上", "在某种意义上", "应该说", "相对而言",
)

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;…\n]+")
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,}")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_HANDLE_OR_TAG = re.compile(r"[@#][A-Za-z0-9_一-鿿]{1,50}")
_QUOTED = re.compile(r"[「『“\"]([^」』”\"]{1,40})[」』”\"]")
#: 抽象名词后缀。只用于近似指标，见模块 docstring。
_ABSTRACT_SUFFIX = re.compile(r"[一-鿿]{2}(性|化|度|力|感|主义)")

#: 英语里高频出现的功能词。`_LATIN_TOKEN` 会把它们一起收进来，
#: 而 "the" 不是"具体实体"。这份小表把它摘掉：
#: 宁可少算几个实体，也不要把功能词算成"具体"。
_LATIN_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "have", "has",
        "are", "was", "were", "you", "your", "but", "not", "can", "will",
        "just", "they", "them", "their", "there", "here", "what", "when",
        "how", "why", "who", "all", "any", "one", "two", "our", "its",
        "it", "is", "of", "to", "in", "on", "at", "by", "as", "be", "or",
    }
)

#: 本模块里所有**近似**指标的名字。报告会据此提醒"别单独用这些数"。
APPROXIMATE_METRICS: tuple[str, ...] = ("abstract_noun_density_approx",)


class AiStyleReport(BaseModel):
    """一条正文的确定性风格指标。

    `human_ai_style_rating` 永远是 `unavailable`——本模块**不产生真值**。
    字段放在这里而不是只放在标注记录里，是为了让"这个位置该有一个人工评分"
    出现在每一份自动报告上，而不是只出现在有人记得去填的地方。
    """

    model_config = ConfigDict(extra="forbid")

    available: bool = True

    # --- 规模 ---
    char_count: int = 0
    sentence_count: int = 0

    # --- 句长 ---
    avg_sentence_length: MetricValue = UNAVAILABLE
    sentence_length_variance: MetricValue = UNAVAILABLE
    sentence_length_stdev: MetricValue = UNAVAILABLE

    # --- 套话 ---
    generic_phrase_count: int = 0
    generic_phrases: list[str] = Field(default_factory=list)
    hedge_count: int = 0

    # --- 具体性 ---
    concrete_entity_count: int = 0
    concrete_entities: list[str] = Field(default_factory=list)
    number_count: int = 0
    concrete_entity_density: MetricValue = UNAVAILABLE

    # --- 语气 ---
    question_count: int = 0
    exclamation_count: int = 0

    # --- 重复 ---
    repeated_pattern_count: int = 0
    repeated_patterns: list[str] = Field(default_factory=list)
    repeated_opener_count: int = 0

    # --- 近似（名字带 _approx）---
    abstract_noun_density_approx: MetricValue = UNAVAILABLE

    approximate_metrics: list[str] = Field(default_factory=lambda: list(APPROXIMATE_METRICS))

    # --- 人工真值（本模块不填）---
    human_ai_style_rating: MetricValue = UNAVAILABLE

    def unavailable_metrics(self) -> list[str]:
        names = (
            "avg_sentence_length", "sentence_length_variance", "sentence_length_stdev",
            "concrete_entity_density", "abstract_noun_density_approx",
        )
        return [n for n in names if not isinstance(getattr(self, n), (int, float))]


def split_sentences(text: str) -> list[str]:
    """切句。中英标点一起切，切成空串的段丢掉。

    不引第三方分句器：本项目的正文是短帖（Persona.max_length = 280），
    标点切分在这个尺度上足够，而多一个依赖就多一处版本风险。
    """
    return [piece.strip() for piece in _SENTENCE_SPLIT.split(text or "") if piece.strip()]


def find_generic_phrases(text: str) -> list[str]:
    """命中的套话原文。大小写不敏感（英文词表用小写写的）。"""
    lowered = (text or "").lower()
    return [phrase for phrase in GENERIC_PHRASES if phrase.lower() in lowered]


def count_hedges(text: str) -> int:
    return sum((text or "").count(hedge) for hedge in HEDGES)


def find_concrete_entities(text: str) -> list[str]:
    """具体实体的**去重**列表。

    四类：数字、拉丁词（去掉英语功能词）、@handle/#hashtag、引号内的专名。
    去重是刻意的：一段话里 "GPT-5" 出现五次，具体性并不因此翻五倍——
    它仍然是**一个**具体的指称。重复本身由 `repeated_pattern_count` 管。
    """
    text = text or ""
    found: set[str] = set()

    for match in _NUMBER.finditer(text):
        found.add(match.group(0))
    for match in _LATIN_TOKEN.finditer(text):
        token = match.group(0)
        if token.lower() not in _LATIN_STOPWORDS:
            found.add(token)
    for match in _HANDLE_OR_TAG.finditer(text):
        found.add(match.group(0))
    for match in _QUOTED.finditer(text):
        found.add(match.group(1).strip())

    return sorted(found)


def find_repeated_patterns(text: str, *, n: int = 4, threshold: int = 3) -> list[str]:
    """重复出现的 n 字片段（去掉空白后按字符滑窗）。

    为什么按字符而不是按词：中文没有空格，按词要先分词；而"重复的 4 个字"
    已经足够抓住模板感的来源（同一句式的反复使用）。
    `threshold=3` 是刻意的低门槛：出现两次可能是修辞，出现三次就是套路。
    """
    stripped = re.sub(r"\s+", "", text or "")
    if len(stripped) < n:
        return []
    counts: dict[str, int] = {}
    for index in range(len(stripped) - n + 1):
        gram = stripped[index : index + n]
        # 纯标点/纯数字的片段不算"句式"。
        if not any(ch.isalpha() or "一" <= ch <= "鿿" for ch in gram):
            continue
        counts[gram] = counts.get(gram, 0) + 1
    return sorted(gram for gram, count in counts.items() if count >= threshold)


def repeated_openers(sentences: Iterable[str], *, width: int = 4) -> int:
    """以相同前缀开头的句子数（超过 1 才算重复）。

    这是"模板感"最直观的一个信号：每段都以"首先/其次/另外"开头，
    句式就固定了。取前 4 个字符，是长度与噪声之间的一个折中。
    """
    counts: dict[str, int] = {}
    for sentence in sentences:
        head = sentence[:width]
        if len(head) < width:
            continue
        counts[head] = counts.get(head, 0) + 1
    return sum(count - 1 for count in counts.values() if count > 1)


def abstract_noun_density(text: str) -> MetricValue:
    """抽象名词密度（**近似**）。

    分子是后缀匹配（性/化/度/力/感/主义）的命中数，分母是总字数 × 100。
    它对中文只是粗略信号：`温度`、`时代`、`力气` 都会被误算。所以：
    - 名字里带 `_approx`；
    - 出现在 `approximate_metrics` 里；
    - **不**参与任何加权或门禁。
    """
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return UNAVAILABLE
    hits = len(_ABSTRACT_SUFFIX.findall(text))
    return hits / len(text) * 100


def analyze_style(text: str) -> AiStyleReport:
    """算一条正文的全部确定性风格指标。纯函数，无 IO，无 LLM。"""
    text = text or ""
    if not text.strip():
        # 空文本：没有任何指标是可算的。返回全 unavailable 的报告，
        # 而不是一堆 0——0 会被聚合进平均值，把"没测"稀释成"很好"。
        return AiStyleReport(available=False, char_count=0, sentence_count=0)

    sentences = split_sentences(text)
    lengths = [len(s) for s in sentences]
    if lengths:
        avg: MetricValue = statistics.fmean(lengths)
        variance: MetricValue = statistics.pvariance(lengths)
        stdev: MetricValue = statistics.pstdev(lengths)
    else:
        avg = variance = stdev = UNAVAILABLE

    entities = find_concrete_entities(text)
    repeated = find_repeated_patterns(text)

    return AiStyleReport(
        available=True,
        char_count=len(text),
        sentence_count=len(sentences),
        avg_sentence_length=avg,
        sentence_length_variance=variance,
        sentence_length_stdev=stdev,
        generic_phrase_count=len(find_generic_phrases(text)),
        generic_phrases=find_generic_phrases(text),
        hedge_count=count_hedges(text),
        concrete_entity_count=len(entities),
        concrete_entities=entities[:40],
        number_count=len(_NUMBER.findall(text)),
        concrete_entity_density=rate(len(entities) * 100, len(text)),
        question_count=text.count("?") + text.count("？"),
        exclamation_count=text.count("!") + text.count("！"),
        repeated_pattern_count=len(repeated),
        repeated_patterns=repeated[:20],
        repeated_opener_count=repeated_openers(sentences),
        abstract_noun_density_approx=abstract_noun_density(text),
    )


def human_edit_distance(draft: str, edited: str | None) -> MetricValue:
    """人工改写与 Agent 草稿的**归一化**编辑距离（0 = 一字未改，1 = 完全不同）。

    为什么归一化：原始 Levenshtein 距离随文本长度增长，280 字的稿子改 20 字
    和 80 字的稿子改 20 字，意义完全不同。除以两串长度的较大值之后，
    这个数才是一个能跨稿子比较的比例。

    为什么它重要：这是整份内容评估里**唯一**由人产生的、直接反映
    "Agent 写的东西离能用有多远"的指标。别的指标都在猜 AI 味，这个不是猜的
    ——它是人事后改了多少。

    没有人工改写（没进审核 / approve 未改 / reject）时返回 unavailable，
    不是 0。0 会被读成"改都不用改"，那是最乐观的一种误读。
    """
    if edited is None:
        return UNAVAILABLE
    a, b = draft or "", edited or ""
    if not a and not b:
        return UNAVAILABLE

    # 经典 DP。帖子长度在 280 字量级，O(n*m) 完全够用。
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,        # 删
                    current[j - 1] + 1,     # 增
                    previous[j - 1] + (ca != cb),  # 改
                )
            )
        previous = current
    return previous[-1] / max(len(a), len(b))
