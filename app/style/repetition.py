"""跨帖重复检测：这条新稿和最近写过/发过的东西有多像。

## 为什么需要它

X 把"重复或实质相似的内容"明确定性为 spam（copypasta），这是无人值守发布
最现实的封号理由——而它恰好是模型最容易犯的：同一个选题、同一套说法，
换个开头就是一条"新"帖子。

本模块出现之前，仓库里唯一的重复检查是 `app/agent/nodes/guardrail.py` 里的
精确字符串匹配（只做 `.strip()`），改一个字就绕过，而且它是**提示性**的——
发布路径从不调它。也就是说，一条重复内容被人批准过，它照样发得出去。

## 三层，权重不同

| 层 | 手段 | 定位 |
|---|---|---|
| `verbatim` | 复用 `app/style/spans.py` 的 n-gram（`SHARED_SPAN = 12`） | **硬** |
| `lexical` | 小 n 字符 n-gram containment | **仅提示** |
| `topic` | 题目归一化后精确匹配 | **硬** |

**为什么 lexical 只提示不拦截。** 中文短文本即便毫不相关也有很高的字符重合
（虚词、常见搭配到处都是），而字符 n-gram 建模的是**表面形式**——它对"同一个
意思换个说法"恰恰是盲的，而那正是最可能发生的失败模式。调小 n 误报不可用
（推文太短，分母太小），调到 12 就塌回 verbatim 那一层。没有一个能站稳的区间，
所以它的取值只记进 `checks` 供人观察，**不让它单独拦下任何东西**。

**为什么 topic 是主力。** 真正会重复的帖子，题目几乎必然复用——而题目串短、
离散、可比。它在影子模式下尤其不可替代：那期间什么都不发布，
基于正文的检测拿到的是一份空语料（见 `list_recent_draft_contents` 的说明）。

## 它测不出来的（如实记入 limitations）

- 真·语义改写（换掉全部措辞，讲同一件事）；
- 同一个主张换一批证据；
- "同一件事换个角度"——题目串不同，底下的事件是同一个。

项目禁 ML 依赖，不做 embedding，这三样就是测不了。兜底是影子模式加人。

## 纯函数契约

无 IO、无随机、不取挂钟（时间窗口由调用方在用 SQL 取语料时就切好了，
所以这里连 `now` 都不需要）、不读配置——阈值由调用方以参数传入。
同 `app/style/spans.py` / `app/style/features.py`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.style.spans import SHARED_SPAN, corpus_spans, overlapping_spans, spans_of

#: lexical 层的 n。见模块说明：取 8 是"误报可忍、又能看见大段改写"的折中，
#: 而它**只用于提示**，所以取值的风险远小于把它当闸门。
LEXICAL_N = 8

#: lexical 层的最短正文长度。短文本的 n-gram 集合太小，containment 会饱和
#: （40 字的稿子总共才 33 个 span，随便撞几个比例就很高），所以短稿不给这个数。
LEXICAL_MIN_LENGTH = 40

#: 题目时效的默认窗口（天）。由调用方传 since，这里只留一个默认值供文档与测试使用。
TOPIC_RECENCY_DAYS = 14

_WHITESPACE = re.compile(r"\s+")

LAYER_VERBATIM = "verbatim"
LAYER_LEXICAL = "lexical"
LAYER_TOPIC = "topic"


@dataclass(frozen=True)
class RepetitionHit:
    """一条命中的重复。`evidence` 是命中的**原文**（片段或题目本身）。

    带上原文而不是只说"重复了"，是沿用 `GateViolation.detail` 的既有做法：
    人看到拦下的理由时要能一眼确认"真的是这一段"，否则只能去猜。
    """

    layer: str
    evidence: str
    detail: str


@dataclass(frozen=True)
class RepetitionResult:
    """三层检测的完整结果。

    `hits` 非空 ⇒ 该拒绝。`lexical_containment` 只是记录，**不参与判断**。
    """

    hits: tuple[RepetitionHit, ...] = ()
    #: 与近期语料的最大字符 n-gram containment（0.0–1.0）。语料为空或正文过短时为 None。
    lexical_containment: float | None = None
    #: 最像的那条语料的开头（截断），供人核对。没有可比对象时为 None。
    lexical_nearest: str | None = None

    @property
    def blocks(self) -> bool:
        return bool(self.hits)


def normalize_topic(topic: str | None) -> str:
    """题目的比较形式：去首尾空白、折叠中间空白、统一大小写。

    只做这三点。再往下（去标点、去停用词、词干化）就开始制造"看起来一样、
    其实是两个题目"的假命中，而题目串本来就是给人读的短句。
    """
    return _WHITESPACE.sub(" ", (topic or "").strip()).casefold()


def detect_verbatim(
    candidate: str,
    corpus: Sequence[str],
    *,
    n: int = SHARED_SPAN,
) -> list[RepetitionHit]:
    """逐字/近逐字搬运：候选与近期正文共享一段长度 n 的连续原文。

    `spans.py` 已经先去掉空白再切片，所以"把原文按行拆开重排"这种最省事的
    绕过方式在这里就被挡住了（那是它模块说明里写明的设计目的之一）。

    语料一次算好 span 集合再逐条复用，而不是每条正文重算一遍——
    这是 `spans.overlapping_spans` 存在的理由，不要在调用方重新发明。
    """
    if not candidate or not corpus:
        return []
    shared = overlapping_spans(candidate, corpus_spans(corpus, n), n)
    if not shared:
        return []
    return [
        RepetitionHit(
            layer=LAYER_VERBATIM,
            evidence=shared[0],
            detail=(
                f"正文与最近写过的内容有 {len(shared)} 处「{n} 字以上」完全相同的片段"
                f"（例如「{shared[0]}」）。重复发布相同或高度相似的内容会被 X 判为"
                "垃圾信息——换一个说法，或者换一件事讲。"
            ),
        )
    ]


def detect_topic_recency(
    topic: str | None,
    recent_topics: Iterable[str],
) -> list[RepetitionHit]:
    """选题时效：这个题目最近已经用过。

    归一化后**精确**匹配。刻意不做相似度：题目串短，模糊匹配会把"同一个模型的
    两个不同角度"判成重复，而这类拒绝的代价（一条本来能发的稿子被拦下）
    虽然小，但会累积成"闸门老是误拦"的印象，最终让人把闸门关掉。
    """
    key = normalize_topic(topic)
    if not key:
        return []
    for recent in recent_topics:
        if normalize_topic(recent) == key:
            return [
                RepetitionHit(
                    layer=LAYER_TOPIC,
                    evidence=(topic or "").strip(),
                    detail=(
                        f"最近已经写过同一个题目（「{(topic or '').strip()}」）。"
                        "同一个点反复讲，即便措辞不同，读者看到的也是重复内容——"
                        f"换一个题目，或者等 {TOPIC_RECENCY_DAYS} 天之后再说。"
                    ),
                )
            ]
    return []


def compute_lexical_containment(
    candidate: str,
    corpus: Sequence[str],
    *,
    n: int = LEXICAL_N,
    min_length: int = LEXICAL_MIN_LENGTH,
) -> tuple[float | None, str | None]:
    """候选被近期语料覆盖的最大比例，以及最像的那条（截断）。**仅供参考。**

    返回 `(None, None)` 表示这条路算不出有意义的数：正文太短（见
    `LEXICAL_MIN_LENGTH`）或语料为空。此时**不要**把 None 当成 0——
    "没有可比对象"和"完全不像"在下游是两件事（同 5C 的 `NULL ≠ 0`）。
    """
    if len(candidate or "") < min_length or not corpus:
        return None, None

    candidate_spans = spans_of(candidate, n)
    if not candidate_spans:
        return None, None

    best_ratio = 0.0
    best_text: str | None = None
    for text in corpus:
        if not text:
            continue
        overlap = len(candidate_spans & spans_of(text, n))
        if not overlap:
            continue
        ratio = overlap / len(candidate_spans)
        if ratio > best_ratio:
            best_ratio = ratio
            best_text = text
    if best_text is None:
        return 0.0, None
    return best_ratio, best_text[:60]


def evaluate_repetition(
    *,
    content: str | None,
    topic: str | None,
    recent_contents: Sequence[str] = (),
    recent_topics: Iterable[str] = (),
    span: int = SHARED_SPAN,
) -> RepetitionResult:
    """三层一起跑。`hits` 里的每一条都是**硬**的；lexical 只带回来记录。

    `recent_contents` 应当同时包含近期已发布正文与近期草稿正文——
    理由见 `app/repositories.list_recent_draft_contents`。
    """
    text = content or ""
    hits = [
        *detect_verbatim(text, recent_contents, n=span),
        *detect_topic_recency(topic, recent_topics),
    ]
    containment, nearest = compute_lexical_containment(text, recent_contents)
    return RepetitionResult(
        hits=tuple(hits),
        lexical_containment=containment,
        lexical_nearest=nearest,
    )


__all__ = [
    "LAYER_LEXICAL",
    "LAYER_TOPIC",
    "LAYER_VERBATIM",
    "LEXICAL_MIN_LENGTH",
    "LEXICAL_N",
    "TOPIC_RECENCY_DAYS",
    "RepetitionHit",
    "RepetitionResult",
    "compute_lexical_containment",
    "detect_topic_recency",
    "detect_verbatim",
    "evaluate_repetition",
    "normalize_topic",
]
