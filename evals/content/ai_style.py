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

## Phase 5B.2：算法原语已上移到 `app/style/features.py`

同一批原语现在两边都要用——评估侧拿它诊断稿子，生产侧拿它从真实样本里统计
"人是怎么写的"。抄一份会在某天漂成两套口径，所以定义搬到了 `app/`，
本模块**只剩适配**：把 `app` 侧返回的朴素 `float | None` 映射成
`MetricValue` / `UNAVAILABLE`。

方向是刻意的单向：`evals → app`。所以 `MetricValue` / `UNAVAILABLE` / `rate`
**留在这里**，没有跟着原语一起走——把"算不出来就说算不出来"这个评估契约
搬进生产代码，既是反向依赖，也会把它复制成两份。
"""

from __future__ import annotations

import statistics

from pydantic import BaseModel, ConfigDict, Field

from app.style.features import (
    APPROXIMATE_METRICS,
    GENERIC_PHRASES,
    HEDGES,
    abstract_noun_density,
    count_hedges,
    count_numbers,
    find_concrete_entities,
    find_generic_phrases,
    find_repeated_patterns,
    repeated_openers,
    split_sentences,
)
from evals.values import UNAVAILABLE, MetricValue, rate

__all__ = [
    "APPROXIMATE_METRICS",
    "GENERIC_PHRASES",
    "HEDGES",
    "AiStyleReport",
    "abstract_noun_density",
    "analyze_style",
    "count_hedges",
    "find_concrete_entities",
    "find_generic_phrases",
    "find_repeated_patterns",
    "human_edit_distance",
    "repeated_openers",
    "split_sentences",
]

#: `float | None` → `MetricValue`。None（生产侧口径的"算不出来"）映射成
#: `UNAVAILABLE`（评估侧口径的同一件事）。集中在一处，免得每个调用点
#: 各写一遍 `or UNAVAILABLE`——那样迟早有人漏一个，让一个 None 混进聚合。
def _metric(value: float | None) -> MetricValue:
    return UNAVAILABLE if value is None else value


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
    generic = find_generic_phrases(text)

    return AiStyleReport(
        available=True,
        char_count=len(text),
        sentence_count=len(sentences),
        avg_sentence_length=avg,
        sentence_length_variance=variance,
        sentence_length_stdev=stdev,
        generic_phrase_count=len(generic),
        generic_phrases=generic,
        hedge_count=count_hedges(text),
        concrete_entity_count=len(entities),
        concrete_entities=entities[:40],
        number_count=count_numbers(text),
        concrete_entity_density=rate(len(entities) * 100, len(text)),
        question_count=text.count("?") + text.count("？"),
        exclamation_count=text.count("!") + text.count("！"),
        repeated_pattern_count=len(repeated),
        repeated_patterns=repeated[:20],
        repeated_opener_count=repeated_openers(sentences),
        abstract_noun_density_approx=_metric(abstract_noun_density(text)),
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
