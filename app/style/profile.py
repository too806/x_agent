"""从一批真实样本算出 StyleProfile（Phase 5B.2 工作单第十七节）。

## 纯函数，可复现

`compute_profile()` 是**确定性**的：同一批样本必然得到同一个 Profile，
逐字段相同。这不是"应该如此"，而是 Smoke 5 直接测的东西，也是整条
"从真实统计里提炼结构"链路的前提——一个会漂的 Profile 无法被引用、
无法被比较、也无法解释。

所以这里不出现：随机、挂钟时间、集合遍历顺序、字典插入顺序相关的输出。
（分布用 `sorted()` 归一化键序，分位数用显式排序。）

## 每个数都要能回答"怎么算的"

工作单第十七节要求 Profile 是**真实统计**，不是模型的一句"这个账号很自然"。
所以每个指标旁边都写清了分子分母——`reasons` 的形态照抄
`app/agent/trends.py::_explain`：带具体数字，人能回原始样本核对。

## 确定性特征一律不落库

Profile 里所有从正文算出来的数，都能从 `content` 重算（见
`app/style/features.py`）。`StyleProfileFeatures` 存进库是一次**快照**，
不是唯一真相——重算得到同一个值，正是它可信的理由。
"""

from __future__ import annotations

from typing import Protocol, Sequence

from app.style import features
from app.style.schemas import StyleProfileFeatures
from app.style.taxonomy import HookType, StructureType


class SampleLike(Protocol):
    """`compute_profile` 认的最小样本形状。

    用 Protocol 而不是直接吃 ORM 行：这一层只需要"正文 + 两个语义标签"，
    让它依赖整个 `StyleSample` 模型会把"算 Profile"和"数据库是什么形状"
    绑在一起，而这两件事的变更节奏完全不同。ORM 行天然满足这个 Protocol。
    """

    content: str
    structure_type: str | None
    hook_type: str | None


def compute_profile(samples: Sequence[SampleLike]) -> StyleProfileFeatures:
    """算一批样本的统计特征。纯函数：无 IO、无随机、无挂钟时间。

    空样本返回一个 `sample_count=0` 且各统计量为 `None` 的 Profile，
    **不抛异常**：采集规模不足是要如实记录的事实（工作单第二十九条），
    不是错误。调用方拿到 `None` 就知道"这批不够"，而不是被一个异常打断。
    """
    if not samples:
        return StyleProfileFeatures(sample_count=0)

    texts = [sample.content or "" for sample in samples]
    lengths = [len(text) for text in texts]
    paragraph_counts = [len(features.split_paragraphs(text)) for text in texts]
    sentence_counts = [len(features.split_sentences(text)) for text in texts]

    # 句长分布按**所有样本的所有句子**汇总，而不是"每条样本的平均句长"——
    # 后者会把长稿的句子权重压低，让分布偏向短帖。
    all_sentence_lengths: list[int] = []
    for text in texts:
        all_sentence_lengths.extend(features.sentence_lengths(features.split_sentences(text)))

    return StyleProfileFeatures(
        sample_count=len(samples),
        median_length=features.median(lengths),
        p25_length=features.percentile(lengths, 25),
        p75_length=features.percentile(lengths, 75),
        median_paragraphs=features.median(paragraph_counts),
        mean_sentence_count=features.mean_or_none(sentence_counts),
        # 密度类各自按"每条样本算比率，再取中位数"，而不是"总和 / 总字数"：
        # 后者会被最长的几条样本主导。中位数对单条超长样本不敏感，
        # 而我们关心的是"典型的一条长什么样"。
        emoji_density=_median_of(texts, features.emoji_density),
        question_density=_median_of(texts, features.question_density),
        number_density=_median_of(texts, features.number_density),
        english_ratio=_median_of(texts, features.english_ratio),
        structure_distribution=features.distribution(
            _label(sample.structure_type, StructureType.UNCLASSIFIED.value)
            for sample in samples
        ),
        hook_distribution=features.distribution(
            # 用 HookType 自己的 UNCLASSIFIED：两个枚举的这个值当前恰好都是
            # "unclassified"，但借另一个枚举的常量来填这一栏，等于把"两个词
            # 拼写相同"变成一条隐式依赖——哪天结构分类法改了默认值，
            # hook 的分布会跟着一起变，而改动点在别处。
            _label(sample.hook_type, HookType.UNCLASSIFIED.value) for sample in samples
        ),
        opening_distribution=features.distribution(
            features.opening_pattern(text) for text in texts
        ),
        closing_distribution=features.distribution(
            features.closing_pattern(text) for text in texts
        ),
        sentence_length_distribution=_sentence_length_distribution(all_sentence_lengths),
    )


def features_from_row(row: object) -> StyleProfileFeatures:
    """`style_profile` 表的一行 → `StyleProfileFeatures`。

    逐字段显式搬运，**不用** `from_orm` / `model_validate`：那两者在列名与
    字段名不一致时会静默取默认值，于是"Profile 里这个数是哪来的"会变成
    "它其实是 0，因为没人搬过来"。显式列出来，漏一个就是明显的空白。
    """
    return StyleProfileFeatures(
        sample_count=getattr(row, "sample_count", 0) or 0,
        median_length=getattr(row, "median_length", None),
        p25_length=getattr(row, "p25_length", None),
        p75_length=getattr(row, "p75_length", None),
        median_paragraphs=getattr(row, "median_paragraphs", None),
        mean_sentence_count=getattr(row, "mean_sentence_count", None),
        emoji_density=getattr(row, "emoji_density", None),
        question_density=getattr(row, "question_density", None),
        number_density=getattr(row, "number_density", None),
        english_ratio=getattr(row, "english_ratio", None),
        structure_distribution=dict(getattr(row, "structure_distribution", None) or {}),
        hook_distribution=dict(getattr(row, "hook_distribution", None) or {}),
        opening_distribution=dict(getattr(row, "opening_distribution", None) or {}),
        closing_distribution=dict(getattr(row, "closing_distribution", None) or {}),
        sentence_length_distribution=dict(
            getattr(row, "sentence_length_distribution", None) or {}
        ),
    )


def _label(value: str | None, default: str) -> str:
    """没标注过的样本落到 `unclassified`，而不是被丢掉。

    丢掉会让分布的分母与实际样本数对不上，于是"这条分布基于几条样本"
    变得没有答案。落进 unclassified 至少是诚实的：这个类别就是"没归进去"。
    """
    return value or default


def _median_of(texts: Sequence[str], measure) -> float | None:
    """对每条文本算一个比率，取中位数。算不出来的样本（分母为 0）被跳过。

    全部算不出来时返回 `None`。跳过而不是当成 0：一条空文本的"每百字 emoji 数"
    不存在，不是 0。
    """
    values = [value for value in (measure(text) for text in texts) if value is not None]
    return features.median(values)


def _sentence_length_distribution(lengths: Sequence[int]) -> dict[str, float]:
    """句长分位。空列表返回空 dict（而不是三个 None）——
    下游 `StyleRecipe` 要据此判"句子该多长"，空 dict 让"没有数据"
    与"分位是 0"在类型上就分得开。"""
    if not lengths:
        return {}
    return {
        "p25": features.percentile(lengths, 25) or 0.0,
        "p50": features.median(lengths) or 0.0,
        "p75": features.percentile(lengths, 75) or 0.0,
    }


__all__ = ["SampleLike", "compute_profile", "features_from_row"]
