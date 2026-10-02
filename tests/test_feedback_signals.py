"""`app/feedback/signals.py` 与 `report.py`：信号、拒因果、可追溯。

这里最值得测的不是"信号算得对不对"，而是**它在什么情况下什么都不说**：
样本不足时、没有依据时、缺字段时。一个只会说好听话的反馈层，
比没有反馈层更危险。
"""

from __future__ import annotations

import pytest

from app.feedback import report as report_mod
from app.feedback import signals as signals_mod
from app.feedback.schema import FeedbackSignalKind, SnapshotObservation
from app.feedback.signals import (
    MIN_SAMPLES_FOR_COMPARISON,
    CausalLanguageError,
    InstructionalLanguageError,
    assert_no_causal_claim,
    assert_observational,
    build_signals,
    find_causal_language,
    find_instruction_language,
)


def obs(sid: str, **counts) -> SnapshotObservation:
    return SnapshotObservation(
        snapshot_id=sid,
        x_post_id=f"x{sid}",
        published_post_id="p1",
        hours_since_publish=counts.pop("hours", 3.0),
        **counts,
    )


def full_batch(n: int, *, likes=10, replies=2, reposts=1, quotes=1) -> list[SnapshotObservation]:
    return [
        obs(f"s{i}", likes=likes + i, replies=replies, reposts=reposts, quotes=quotes)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 因果 / 指令措辞检查
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "点赞高是因为开头写得好。",
        "这条内容导致了更多转发。",
        "数据证明了短句更受欢迎。",
        "This proves short posts work.",
        "It leads to more replies.",
    ],
)
def test_causal_markers_are_found(text):
    assert find_causal_language(text)


def test_observational_wording_passes_the_causal_guard():
    assert find_causal_language("观察到 6 条观察的点赞数中位数是 12。") == []
    assert_no_causal_claim("观察到 6 条观察的点赞数中位数是 12。")


def test_causal_guard_raises_rather_than_warning():
    """命中是编程错误——静默记一条日志，就等于让它进了发给模型的摘要。"""
    with pytest.raises(CausalLanguageError):
        assert_no_causal_claim("点赞高是因为开头写得好。")


@pytest.mark.parametrize(
    "text",
    ["你应该写得短一点", "Next time use more numbers", "建议下次用这个结构"],
)
def test_instruction_markers_are_found(text):
    assert find_instruction_language(text)


def test_instructional_guard_raises():
    with pytest.raises(InstructionalLanguageError):
        assert_observational("你应该写得短一点")


# ---------------------------------------------------------------------------
# 样本下限
# ---------------------------------------------------------------------------


def test_no_observations_yields_no_signals():
    """没有依据就没有信号——不是一条"暂无数据"的信号。"""
    assert build_signals([]) == ()


def test_below_threshold_yields_exactly_one_insufficient_sample():
    observations = full_batch(MIN_SAMPLES_FOR_COMPARISON - 1)
    signals = build_signals(observations)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.kind is FeedbackSignalKind.INSUFFICIENT_SAMPLE
    # 只带 n，不带任何比较、比率或排名
    assert signal.value == float(MIN_SAMPLES_FOR_COMPARISON - 1)
    assert signal.comparison_group is None
    # 下限是我们的采集门槛，不是统计显著性——必须在文本里说清楚
    assert "统计显著性" in signal.statement
    assert len(signal.evidence) == MIN_SAMPLES_FOR_COMPARISON - 1


def test_at_threshold_the_guard_releases():
    signals = build_signals(full_batch(MIN_SAMPLES_FOR_COMPARISON))
    kinds = {s.kind for s in signals}
    assert FeedbackSignalKind.INSUFFICIENT_SAMPLE not in kinds
    assert FeedbackSignalKind.ATTENTION in kinds


# ---------------------------------------------------------------------------
# 分布信号
# ---------------------------------------------------------------------------


def test_attention_signal_reports_a_distribution_not_a_verdict():
    signals = build_signals(full_batch(5, likes=10))
    attention = next(s for s in signals if s.kind is FeedbackSignalKind.ATTENTION)

    assert attention.metric == "likes"
    assert attention.value == 12.0  # 10,11,12,13,14 → 中位数 12
    assert attention.sample_count == 5
    assert attention.comparison_group is None
    assert attention.confidence is None
    # 措辞必须让人看出这是观察，不是结论
    assert "观察" in attention.statement
    assert "因果" in attention.statement


def test_metrics_with_no_readings_produce_no_signal():
    """一项计数全部缺失时不发信号——一条 value=None 的信号看着像结论。"""
    observations = [obs(f"s{i}", hours=3.0) for i in range(5)]
    signals = build_signals(observations)
    kinds = {s.kind for s in signals}

    assert FeedbackSignalKind.ATTENTION not in kinds
    assert FeedbackSignalKind.CONVERSATION not in kinds
    assert FeedbackSignalKind.SHARE not in kinds
    # 但每个缺的字段都要如实记一笔
    assert FeedbackSignalKind.MISSING_METRIC in kinds


def test_missing_metric_signal_counts_only_the_missing_ones():
    observations = full_batch(5)
    observations[0] = obs("s0", replies=None, likes=1, reposts=1, quotes=1)
    signals = build_signals(observations)
    missing = next(
        s for s in signals if s.kind is FeedbackSignalKind.MISSING_METRIC and s.metric == "replies"
    )
    assert missing.sample_count == 1
    assert missing.evidence == ("s0",)
    assert "不按 0 计入" in missing.statement


def test_share_signal_skips_observations_with_incomplete_counts():
    """分母是一部分、分子是另一部分时算出的"占比"没有意义。"""
    observations = full_batch(5)
    observations[0] = obs("s0", likes=5, replies=None, reposts=1, quotes=1)
    signals = build_signals(observations)
    share = next(s for s in signals if s.kind is FeedbackSignalKind.SHARE)
    assert share.sample_count == 4  # 只在那 4 条齐全的观察上算
    assert "s0" not in share.evidence


# ---------------------------------------------------------------------------
# 报告：出口校验
# ---------------------------------------------------------------------------


def test_report_carries_sample_count_and_limitations():
    report = report_mod.build_report(full_batch(5))
    assert report.sample_count == 5
    assert any("合成单一分数" in note for note in report.limitations)
    assert any("曝光与收藏" in note for note in report.limitations)
    assert any("时间档" in note for note in report.limitations)


def test_report_on_empty_data_says_so_instead_of_reporting_zeros():
    report = report_mod.build_report([])
    assert report.sample_count == 0
    assert report.signals == ()
    assert any("没有可用的观察" in note for note in report.limitations)


def test_report_rejects_a_signal_without_evidence():
    """可追溯性是出口检查：没有依据的信号不许出门。"""
    orphan = signals_mod.FeedbackSignal(
        kind=FeedbackSignalKind.ATTENTION,
        statement="观察到 1 条观察的点赞数中位数是 3。",
        metric="likes",
        value=3.0,
        sample_count=1,
        evidence=(),
    )
    with pytest.raises(ValueError, match="没有依据"):
        report_mod._validate_signals([orphan], full_batch(5))


def test_report_rejects_a_signal_citing_an_unknown_observation():
    dangling = signals_mod.FeedbackSignal(
        kind=FeedbackSignalKind.ATTENTION,
        statement="观察到 1 条观察的点赞数中位数是 3。",
        metric="likes",
        value=3.0,
        sample_count=1,
        evidence=("nope",),
    )
    with pytest.raises(ValueError, match="不存在"):
        report_mod._validate_signals([dangling], full_batch(5))


def test_report_rejects_causal_wording_in_a_statement():
    causal = signals_mod.FeedbackSignal(
        kind=FeedbackSignalKind.ATTENTION,
        statement="点赞多是因为开头好。",
        metric="likes",
        value=3.0,
        sample_count=1,
        evidence=("s0",),
    )
    with pytest.raises(CausalLanguageError):
        report_mod._validate_signals([causal], full_batch(5))


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


def test_feedback_context_is_capped_and_loses_the_body():
    report = report_mod.build_report(full_batch(7))
    context = report_mod.build_feedback_context(report, limit=2)

    assert len(context.signals) == 2
    assert context.snapshot_count == 7
    assert context.post_count == 1
    assert context.latest_observed_at is None  # 这批 observation 没给 observed_at
    # 全部来源 id 保留，供 Run Detail 事后回到原始观察
    assert len(context.source_snapshot_ids) == 7
    # 上下文里没有正文，也没有逐条的点赞数
    assert not hasattr(context, "observations")


def test_empty_context_is_flagged_empty():
    context = report_mod.build_feedback_context(report_mod.build_report([]))
    assert context.is_empty
    assert not report_mod.build_feedback_context(
        report_mod.build_report(full_batch(5))
    ).is_empty
