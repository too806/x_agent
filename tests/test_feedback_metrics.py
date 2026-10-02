"""`app/feedback/metrics.py` 的确定性派生量。

这一层测的就一件事：**未知会不会被写成 0**。每个可能算不出来的数都有一对
用例——"齐全"与"缺一项"，后者的期望值是 `None` 而不是一个更小的数。
"""

from __future__ import annotations

import pytest

from app.feedback import metrics
from app.feedback.metrics import Metrics


def test_full_metrics_derive_every_number():
    m = Metrics(likes=40, replies=6, reposts=3, quotes=1)

    assert metrics.attention(m) == 40
    assert metrics.conversation(m) == 7
    assert metrics.amplification(m) == 3
    assert metrics.engagement_total(m) == 50
    assert metrics.attention_share(m) == pytest.approx(0.8)
    assert metrics.conversation_share(m) == pytest.approx(0.14)
    assert metrics.amplification_share(m) == pytest.approx(0.06)
    assert metrics.reply_rate(m) == pytest.approx(6 / 50)
    assert metrics.repost_rate(m) == pytest.approx(3 / 50)
    assert metrics.quote_rate(m) == pytest.approx(1 / 50)


def test_engagement_total_is_none_when_any_part_is_unknown():
    """**全有才求和**。

    三项已知、一项未知时求和，会把"有一项读不到"变成一个更小的、
    看起来很真实的互动总量。这是本阶段最容易犯、也最难在报告里看出来的错。
    """
    m = Metrics(likes=10, replies=2, reposts=None, quotes=0)
    assert metrics.engagement_total(m) is None
    # 连带地，所有以它为分母的比率也必须是 None——而不是 0.0
    assert metrics.attention_share(m) is None
    assert metrics.conversation_share(m) is None
    assert metrics.reply_rate(m) is None


def test_conversation_is_none_when_either_half_is_unknown():
    assert metrics.conversation(Metrics(replies=3, quotes=None)) is None
    assert metrics.conversation(Metrics(replies=None, quotes=3)) is None
    assert metrics.conversation(Metrics(replies=3, quotes=0)) == 3


def test_rate_guards_the_denominator():
    """分母缺失或 <= 0 → `None`（"比例是多少"没有被定义，不是 0）。"""
    assert metrics.rate(1, None) is None
    assert metrics.rate(1, 0) is None
    assert metrics.rate(1, -3) is None
    assert metrics.rate(None, 5) is None
    assert metrics.rate(3, 6) == 0.5


def test_engagement_rate_is_always_none_without_impressions():
    """v1 只采集公开计数，曝光量恒为未知，互动率必然算不出来。"""
    m = Metrics(likes=100, replies=10, reposts=5, quotes=2)
    assert metrics.engagement_rate(m) is None
    # 就算有人手工塞进一个曝光量，函数本身是对的——留着它是给将来的来源
    assert metrics.engagement_rate(Metrics(likes=1, replies=1, reposts=1, quotes=1,
                                           impressions=100)) == pytest.approx(0.04)
    # 曝光为 0 不产生"0% 互动率"
    assert metrics.engagement_rate(Metrics(likes=1, impressions=0)) is None


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (0.0, "0-1h"),
        (0.999, "0-1h"),
        (1.0, "1-6h"),
        (5.9, "1-6h"),
        (6.0, "6-24h"),
        (23.9, "6-24h"),
        (24.0, "24-72h"),
        (71.9, "24-72h"),
        (72.0, ">72h"),
        (1000.0, ">72h"),
    ],
)
def test_window_bucket_boundaries_are_half_open(hours, expected):
    """档位是 `[lo, hi)`：边界值归**上一档**，不会出现两个档都答"是"。"""
    assert metrics.window_bucket(hours) == expected


def test_window_bucket_unknown_and_inconsistent_are_none():
    """负数说明观察早于发布——是数据不自洽，不该伪装成"发布后第一小时"。"""
    assert metrics.window_bucket(None) is None
    assert metrics.window_bucket(-0.5) is None


def test_metrics_from_observation_duck_typed():
    """从任意带这些属性的对象取数，不做 import（纯模块不该知道持久化形状）。"""

    class Row:
        likes = 5
        replies = 1
        reposts = 0
        quotes = 0
        impressions = None
        bookmarks = None

    m = Metrics.from_observation(Row())
    assert (m.likes, m.replies, m.reposts, m.quotes) == (5, 1, 0, 0)
    assert m.impressions is None
