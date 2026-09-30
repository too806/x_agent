"""`app/style/profile.py`：样本 → StyleProfile。

这里守的是一条**产品级**承诺：同一份 corpus 必然得到同一个 Profile（工作单
Smoke 5）。它之所以必须成立，是因为下游 `StyleRecipe` 的每条理由都要能回指
到一个具体的统计量——一个会漂的 Profile，那些理由就都变成了无法复核的话。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.style import features
from app.style.profile import compute_profile, features_from_row


@dataclass
class Sample:
    """满足 `SampleLike` 的最小样本。刻意不用 ORM 行：这一层不该依赖数据库形状。"""

    content: str
    structure_type: str | None = None
    hook_type: str | None = None


SAMPLE_TEXTS = [
    "为什么要盯着上下文窗口？因为真正的瓶颈不在这里。先把结论放前面：多数团队的浪费在数据管线。",
    "2024 年，推理成本降了 90%。这个数字背后不是模型变小了，是调度变聪明了。\n\n"
    "所以别再按参数量选模型。",
    "「AI 不是魔法」，这句话只对了一半。它确实不是魔法，但它也不只是统计拟合——" "中间那层工程被所有人忽略了。",
]


# ---------------------------------------------------------------------------
# 空语料：如实记录，不抛异常
# ---------------------------------------------------------------------------


def test_empty_corpus_returns_a_profile_with_zero_samples():
    """采集规模不足是要如实记录的事实（工作单第二十九条），不是错误。

    抛异常会把"这批没采够"变成一个中断整条链路的事故，而它本来只需要
    在报告里写一句 sample_count=0。
    """
    profile = compute_profile([])
    assert profile.sample_count == 0
    assert profile.median_length is None
    assert profile.structure_distribution == {}
    assert profile.sentence_length_distribution == {}


# ---------------------------------------------------------------------------
# 复现性
# ---------------------------------------------------------------------------


def test_same_corpus_yields_a_bit_identical_profile():
    """Smoke 5 的单测形态：同输入两次，逐字段相同。"""
    samples = [Sample(text, "opinion_first", "question") for text in SAMPLE_TEXTS]
    assert compute_profile(samples).model_dump() == compute_profile(samples).model_dump()


def test_profile_does_not_depend_on_sample_order():
    """样本顺序不该影响结果。

    这条挡的是"第一条样本决定了什么"这类 bug——比如用第一条的长度当
    中位数、或者分布按插入顺序渲染。真实采集回来的顺序每次都不同，
    如果结果跟着变，Profile 就不可比较了。
    """
    samples = [Sample(text, "fact_first", "statistic") for text in SAMPLE_TEXTS]
    forward = compute_profile(samples).model_dump()
    backward = compute_profile(list(reversed(samples))).model_dump()
    assert forward == backward


# ---------------------------------------------------------------------------
# 统计量
# ---------------------------------------------------------------------------


def test_scalar_features_track_the_samples():
    samples = [Sample(text) for text in SAMPLE_TEXTS]
    profile = compute_profile(samples)

    lengths = [len(text) for text in SAMPLE_TEXTS]
    assert profile.sample_count == 3
    assert profile.median_length == pytest.approx(features.median(lengths))
    assert profile.p25_length == pytest.approx(features.percentile(lengths, 25))
    assert profile.p75_length == pytest.approx(features.percentile(lengths, 75))
    # p25 <= 中位数 <= p75：分位算错了这条会立刻红
    assert profile.p25_length <= profile.median_length <= profile.p75_length


def test_sentence_length_distribution_covers_every_sentence():
    """句长分布按**所有样本的所有句子**汇总，而不是"每条样本的平均句长"。

    后者会把长稿的句子权重压低，让分布偏向短帖——而"句子该多长"恰恰是
    长稿更该回答的问题。
    """
    samples = [Sample(text) for text in SAMPLE_TEXTS]
    profile = compute_profile(samples)

    all_lengths: list[int] = []
    for text in SAMPLE_TEXTS:
        all_lengths.extend(features.sentence_lengths(features.split_sentences(text)))

    assert profile.sentence_length_distribution["p50"] == pytest.approx(
        features.median(all_lengths)
    )
    assert profile.sentence_length_distribution["p25"] <= (
        profile.sentence_length_distribution["p75"]
    )


def test_distributions_sum_to_one():
    samples = [Sample(text, "opinion_first", "question") for text in SAMPLE_TEXTS]
    profile = compute_profile(samples)
    assert sum(profile.structure_distribution.values()) == pytest.approx(1.0)
    assert sum(profile.opening_distribution.values()) == pytest.approx(1.0)
    assert sum(profile.closing_distribution.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 未标注的样本落进 unclassified，而不是被丢掉
# ---------------------------------------------------------------------------


def test_unannotated_samples_land_in_unclassified():
    """丢掉会让分布的分母与实际样本数对不上。

    "这条分布基于几条样本"必须始终有答案；落进 unclassified 至少是诚实的——
    这个类别就是"没归进去"。
    """
    samples = [Sample("一段没有标注的正文，长度足够。"), Sample("另一段，也没标注。", "contrast")]
    profile = compute_profile(samples)

    assert profile.structure_distribution == {"contrast": 0.5, "unclassified": 0.5}
    assert profile.hook_distribution == {"unclassified": 1.0}
    # 分母是**全部**样本，不是"标注过的那部分"
    assert sum(profile.structure_distribution.values()) == pytest.approx(1.0)


def test_distributions_account_for_every_sample():
    samples = [Sample(f"第 {i} 段正文，长度足够用于统计。", None, "statement") for i in range(4)]
    profile = compute_profile(samples)
    total = sum(
        round(share * 4) for share in profile.structure_distribution.values()
    )
    assert total == 4


# ---------------------------------------------------------------------------
# features_from_row：显式搬运
# ---------------------------------------------------------------------------


class _Row:
    """`style_profile` 表的一行的替身。只给一部分列，缺的走默认。"""

    sample_count = 5
    median_length = 210.0
    p25_length = 150.0
    p75_length = 300.0
    median_paragraphs = 2.0
    mean_sentence_count = 4.5
    emoji_density = 0.0
    question_density = 1.2
    number_density = 3.0
    english_ratio = 0.08
    structure_distribution = {"opinion_first": 0.6, "contrast": 0.4}
    hook_distribution = {"question": 0.6, "statistic": 0.4}
    opening_distribution = {"question": 0.6, "number": 0.4}
    closing_distribution = {"conclusion": 1.0}
    sentence_length_distribution = {"p25": 10.0, "p50": 18.0, "p75": 30.0}


def test_features_from_row_copies_every_field():
    features_out = features_from_row(_Row())
    assert features_out.sample_count == 5
    assert features_out.median_length == 210.0
    assert features_out.structure_distribution == {"opinion_first": 0.6, "contrast": 0.4}
    assert features_out.sentence_length_distribution == {"p25": 10.0, "p50": 18.0, "p75": 30.0}


def test_features_from_row_tolerates_missing_columns():
    """缺列时给默认值，**不抛**——一行老数据不该让整条取 recipe 的路径崩。

    JSONB 列返回 None 是真实存在的（老行、或写入时漏了），
    这里必须把它变成空 dict 而不是让 `dict(None)` 炸掉。
    """

    class _Sparse:
        sample_count = 1

    out = features_from_row(_Sparse())
    assert out.sample_count == 1
    assert out.structure_distribution == {}
    assert out.median_length is None


def test_features_from_row_copies_collections_rather_than_aliasing():
    """搬过来的 dict 必须是副本。

    别名会让下游改了 features 就顺手改了那一行的 JSONB 字段——
    在 ORM 会话里这等于把一个派生值写回数据库。
    """

    class _Row2:
        sample_count = 1
        structure_distribution = {"opinion_first": 1.0}

    out = features_from_row(_Row2())
    out.structure_distribution["contrast"] = 9.0
    assert _Row2.structure_distribution == {"opinion_first": 1.0}
