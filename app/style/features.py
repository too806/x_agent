"""确定性文本特征原语。

这里的东西**只做能算准的**：词表逐字匹配、正则计数、分位数。

## 为什么要从 `evals/content/ai_style.py` 上移

同一批原语（套话词表、切句、具体实体、重复片段）在两边都要用：评估侧用它
诊断"这条稿子有多像 AI 写的"，生产侧用它从真实样本里统计"人是怎么写的"。
抄一份会在某天漂成两套口径，而**两套口径长得一样**是最难发现的错误。

但**不能整模块搬**：`evals/content/ai_style.py` 依赖 `evals/values.py` 的
`MetricValue` / `UNAVAILABLE` / `rate`，那是评估侧的"算不出来就说算不出来"
契约。把那些一起搬进 `app/`，等于造出 `app → evals` 的依赖，
把仓库里唯一的单向箭头（`evals → app`）反过来。

所以这里返回**朴素的 `float | None`**：分母为 0 时给 `None`。评估侧那层薄
适配器负责把 `None` 映射成 `UNAVAILABLE`。哨兵值留在评估侧——生产代码不需要
"unavailable" 这个字符串，它需要的是能参与比较的数和"没算出来"的 None。

## 纯函数契约

无 IO、无随机、不取挂钟时间、不读配置。同一段文本**永远**得到同一个结果。
`StyleProfile` 的可复现性（同一份 corpus → 同一个 Profile）完全建立在
这条契约上，所以这里不许出现任何会随环境变化的东西。
"""

from __future__ import annotations

import math
import re
import statistics
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

#: 套话。命中即计数，命中的词原样回传，方便人核对"这个也算套话？"。
#: 词表是**评估资产**，会随认知变化——改它要同时进位 `evals.__version__`，
#: 否则两份报告会因为口径不同而被误读成"稿子变了"。
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

#: 模板化开头的连接件。命中的是"每段都这么起头"这类句式，
#: 与套话分开：套话是空词，这个是**结构上的固定**。
TEMPLATE_OPENERS: tuple[str, ...] = (
    "首先", "其次", "再次", "最后", "另外", "此外", "同时",
    "第一", "第二", "第三", "其一", "其二", "其三",
)

#: 空洞连接词。比套话更轻，但堆起来同样让稿子失去具体性。
EMPTY_CONNECTIVES: tuple[str, ...] = (
    "也就是说", "与此同时", "在此基础上", "从这个角度看",
    "进一步来说", "更重要的是", "值得注意的是", "正如我们所见",
)

#: 虚假权威措辞。这些词把"我认为"伪装成"业内共识"。
#: **只作为辅助信号**，不是"AI 检测器"——人也会用，只是用得不这么密。
FALSE_AUTHORITY: tuple[str, ...] = (
    "众所周知", "业内人士指出", "有分析认为", "普遍认为", "业界共识",
    "专家表示", "研究表明", "数据显示", "事实证明", "事实上",
)

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;…\n]+")
#: 切句但**把句末标点留在句子里**。表层形态判的正是"这句以什么收尾"——
#: 标点本身是被观察的对象，所以不能像 `_SENTENCE_SPLIT` 那样把它切掉。
#: 用后行断言切分，分隔符归给前一句。
_SENTENCE_SPLIT_KEEPING = re.compile(r"(?<=[。！？!?；;…])|\n")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,}")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_HANDLE_OR_TAG = re.compile(r"[@#][A-Za-z0-9_一-鿿]{1,50}")
_QUOTED = re.compile(r"[「『“\"]([^」』”\"]{1,40})[」』”\"]")
#: 抽象名词后缀。只用于近似指标，见 `abstract_noun_density`。
_ABSTRACT_SUFFIX = re.compile(r"[一-鿿]{2}(性|化|度|力|感|主义)")

#: 常见 emoji 区段。刻意不引第三方 emoji 库：这个项目只需要"有没有、多不多"，
#: 一个区段正则在这个精度上足够，而多一个依赖就多一处版本风险。
_EMOJI = re.compile(
    "["
    "\U0001f300-\U0001f5ff"  # 符号与象形
    "\U0001f600-\U0001f64f"  # 表情
    "\U0001f680-\U0001f6ff"  # 交通与地图
    "\U0001f900-\U0001f9ff"  # 补充符号
    "\U0001fa70-\U0001faff"
    "☀-⛿"
    "✀-➿"
    "]+"
)

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

#: 开头/结尾的**表层**形态。这是确定性的形态分类，不是语义判断——
#: 语义层面的 hook 类型由 LLM 标注（`annotate.py`），两者刻意分开。
OpeningPattern = str  # one of OPENING_PATTERNS
OPENING_PATTERNS: tuple[str, ...] = (
    "question",   # 第一句是个问句
    "number",     # 第一句以数字/年份起头
    "quote",      # 第一句以引号或「」起头
    "address",    # 第一句以"各位/大家/朋友们"起头
    "statement",  # 其余
)

CLOSING_PATTERNS: tuple[str, ...] = (
    "question",   # 末句是问句
    "call",       # 末句是行动号召（"你会…吗""欢迎…"）
    "statement",  # 其余
)

_ADDRESS_PREFIXES: tuple[str, ...] = ("各位", "大家", "朋友们", "同学们", "今天聊聊")
_CALL_MARKERS: tuple[str, ...] = ("欢迎", "不妨", "你会", "你觉得", "一起", "评论区")


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    """切句。中英标点一起切，切成空串的段丢掉。

    不引第三方分句器：本项目的正文是短帖（Persona.max_length = 280），
    标点切分在这个尺度上足够，而多一个依赖就多一处版本风险。
    """
    return [piece.strip() for piece in _SENTENCE_SPLIT.split(text or "") if piece.strip()]


def split_paragraphs(text: str) -> list[str]:
    """按空行切段。单换行**不**算分段：短帖里单换行常只是排版。

    没有任何空行但正文非空时，整段算一个段落（返回长度 1 的列表），
    而不是返回空列表——"一段都没分"和"一片空白"是两件事。
    """
    text = text or ""
    if not text.strip():
        return []
    blocks = [block.strip() for block in _PARAGRAPH_SPLIT.split(text) if block.strip()]
    return blocks or [text.strip()]


# ---------------------------------------------------------------------------
# 计数
# ---------------------------------------------------------------------------


def find_generic_phrases(text: str) -> list[str]:
    """命中的套话原文。大小写不敏感（英文词表用小写写的）。"""
    lowered = (text or "").lower()
    return [phrase for phrase in GENERIC_PHRASES if phrase.lower() in lowered]


def count_hedges(text: str) -> int:
    return sum((text or "").count(hedge) for hedge in HEDGES)


def count_numbers(text: str) -> int:
    """数字 token 数（含小数）。"""
    return len(_NUMBER.findall(text or ""))


def count_questions(text: str) -> int:
    return (text or "").count("?") + (text or "").count("？")


def count_exclamations(text: str) -> int:
    return (text or "").count("!") + (text or "").count("！")


def count_matches(text: str, vocabulary: Sequence[str]) -> int:
    """词表命中次数（可重叠计数，与 `count_hedges` 同口径）。"""
    return sum((text or "").count(item) for item in vocabulary)


def find_concrete_entities(text: str) -> list[str]:
    """具体实体的**去重**列表。

    四类：数字、拉丁词（去掉英语功能词）、@handle/#hashtag、引号内的专名。
    去重是刻意的：一段话里 "GPT-5" 出现五次，具体性并不因此翻五倍——
    它仍然是**一个**具体的指称。重复本身由 `find_repeated_patterns` 管。
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


# ---------------------------------------------------------------------------
# 比率（分母为 0 一律返回 None，绝不返回 0.0）
# ---------------------------------------------------------------------------


def _density(count: int, length: int) -> float | None:
    """每 100 字的密度。长度为 0 时 None。

    不返回 0.0：`0 / 0` 被写成 0 会让"没测"看起来像"测出来是零"，
    而这两件事在聚合时的差别是全部。见 `evals/values.py` 的同一条纪律。
    """
    if length <= 0:
        return None
    return count / length * 100


def _ratio(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return part / whole


def _visible_length(text: str) -> int:
    """去掉空白后的字符数。所有比率的分母都用它，口径统一。"""
    return len(re.sub(r"\s+", "", text or ""))


def abstract_noun_density(text: str) -> float | None:
    """抽象名词密度（**近似**），每 100 字。

    分子是后缀匹配（性/化/度/力/感/主义）的命中数，分母是去空白后的字数。
    它对中文只是粗略信号：`温度`、`时代`、`力气` 都会被误算。所以：
    - 名字里带 `_approx`；
    - 出现在 `approximate_metrics` 里；
    - **不**参与任何加权或门禁。
    """
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return None
    return _density(len(_ABSTRACT_SUFFIX.findall(stripped)), len(stripped))


def emoji_count(text: str) -> int:
    return sum(len(match.group(0)) for match in _EMOJI.finditer(text or ""))


def emoji_density(text: str) -> float | None:
    """每 100 字的 emoji 个数。"""
    return _density(emoji_count(text), _visible_length(text))


def question_density(text: str) -> float | None:
    """每 100 字的问号个数。"""
    return _density(count_questions(text), _visible_length(text))


def number_density(text: str) -> float | None:
    """每 100 字的数字 token 个数。"""
    return _density(count_numbers(text), _visible_length(text))


def english_ratio(text: str) -> float | None:
    """拉丁字母占非空白字符的比例。0 到 1。"""
    visible = _visible_length(text)
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    return _ratio(latin, visible)


# ---------------------------------------------------------------------------
# 分布
# ---------------------------------------------------------------------------


def sentence_lengths(sentences: Iterable[str]) -> list[int]:
    return [len(sentence) for sentence in sentences]


def percentile(values: Sequence[int | float], q: float) -> float | None:
    """线性插值分位数（q 取 0-100）。空序列返回 None。

    刻意不用 `statistics.quantiles`：它要求至少两个数据点，而 corpus 里
    只采到一条样本是完全可能的，那种情况下我们要一个数，不是一个异常。
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * (q / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    fraction = position - lower
    return float(ordered[lower]) * (1 - fraction) + float(ordered[upper]) * fraction


def median(values: Sequence[int | float]) -> float | None:
    return percentile(values, 50)


def mean_or_none(values: Sequence[int | float]) -> float | None:
    if not values:
        return None
    return statistics.fmean(values)


# ---------------------------------------------------------------------------
# 表层形态
# ---------------------------------------------------------------------------


def sentences_with_terminator(text: str) -> list[str]:
    """切句并**保留句末标点**。给表层形态判断用。

    与 `split_sentences` 是两个函数而不是一个开关：那个的语义是"句子的内容"
    （`sentence_lengths`、`repeated_openers` 要的是长度与前缀，标点是噪声），
    这个的语义是"句子连标点长什么样"。合成一个会逼着两边的调用点都去关心
    一个与自己无关的参数。
    """
    return [
        piece.strip()
        for piece in _SENTENCE_SPLIT_KEEPING.split(text or "")
        if piece.strip()
    ]


def opening_pattern(text: str) -> str:
    """第一句的**表层**形态。

    只看形态，不看意思：以问号收尾就是 question，以数字起头就是 number。
    语义层面的"这个开头抓不抓人"由 LLM 的 `hook_type` 回答，两者不重叠。

    用 `sentences_with_terminator` 而不是 `split_sentences`：后者把标点切掉了，
    `first.endswith("?")` 会**永远为假**，于是 question 开头统计不出任何一条
    ——一个静默失效的检测器会把"这个语料里没有问句开头"写进 Profile。
    """
    sentences = sentences_with_terminator(text)
    if not sentences:
        return "statement"
    first = sentences[0].lstrip()
    if first.endswith(("?", "？")):
        return "question"
    if first[:1].isdigit():
        return "number"
    if first[:1] in "「『“\"'":
        return "quote"
    if any(first.startswith(prefix) for prefix in _ADDRESS_PREFIXES):
        return "address"
    return "statement"


def closing_pattern(text: str) -> str:
    """末句的**表层**形态。与 `opening_pattern` 同一套口径。

    行动号召用 `in`（包含）而不是 `endswith`：`_CALL_MARKERS` 是**短语**
    （"欢迎""评论区""不妨"），而它们几乎不会正好落在句末——"欢迎在评论区
    说说你的用法"是一句标准的号召，却以"用法"结尾。用 `endswith` 会让
    call 这一档形同不存在，于是 `EndingType.CALL` 在 recipe 里永远选不出来。
    """
    sentences = sentences_with_terminator(text)
    if not sentences:
        return "statement"
    last = sentences[-1]
    if last.endswith(("?", "？")):
        return "question"
    if any(marker in last for marker in _CALL_MARKERS):
        return "call"
    return "statement"


def distribution(labels: Iterable[str]) -> dict[str, float]:
    """把一串标签统计成"占比分布"。空输入返回空 dict。

    返回占比而不是计数：Profile 要能在不同样本量的 corpus 之间比较，
    计数会被样本量带偏。
    """
    items = [label for label in labels if label]
    if not items:
        return {}
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    total = len(items)
    return {label: count / total for label, count in sorted(counts.items())}
