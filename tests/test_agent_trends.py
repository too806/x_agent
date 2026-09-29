"""trend 打分：确定性、口径、以及"单条帖子撑不起一个趋势"。

工作单第九节要求 `trend_score` **可解释、可复现、可测试**，第二十六节要求
"trend score deterministic"。这个文件就是那两条要求的落点：

- **可复现**：同一输入两次调用必须得到完全相同的对象；改动某项输入，
  只有按定义该变的那一项变。
- **可解释**：每个分项都要能在结果里单独查到（分数是五项加权和，
  不是一句"模型觉得值 90 分"）。
- **单条帖子不能单独制造高可信 trend**：结构不足时封顶，且判定与分数无关。

这些测试全部不碰数据库、不碰网络——打分是纯函数，它必须能被单独证明。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.agent.schemas import TopicLifecycle, TopicQualification
from app.agent.trends import (
    MIN_AUTHORS_FOR_QUALIFIED,
    QUALIFIED_MIN_SCORE,
    SINGLE_SOURCE_SCORE_CAP,
    SPREAD_FULL_AUTHORS,
    STALE_AFTER_HOURS,
    TREND_WEIGHTS,
    TREND_WINDOW_HOURS,
    TopicScanItem,
    observed_engagement,
    qualify,
    rank_key,
    score_novelty,
    score_recency,
    score_source_quality,
    score_spread,
    score_topic,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def post(
    item_id: str,
    *,
    hours_ago: float,
    author: str = "ada",
    likes: int = 0,
    reposts: int = 0,
    replies: int = 0,
    quotes: int = 0,
) -> TopicScanItem:
    """一条观测到的帖子。`hours_ago` 相对于 NOW。"""
    return TopicScanItem(
        item_id=item_id,
        post_id=f"x-{item_id}",
        author=author,
        published_at=NOW - timedelta(hours=hours_ago),
        likes=likes,
        reposts=reposts,
        replies=replies,
        quotes=quotes,
    )


def cluster() -> list[TopicScanItem]:
    """一个正常的、由多位作者在窗口内讨论的话题簇。"""
    return [
        post("ri-1", hours_ago=2, author="ada", likes=40, reposts=5, replies=3, quotes=1),
        post("ri-2", hours_ago=5, author="bob", likes=10, reposts=1),
        post("ri-3", hours_ago=30, author="cara", likes=4),
        post("ri-4", hours_ago=60, author="dan", likes=2),
    ]


# --------------------------------------------------------------------------
# 确定性
# --------------------------------------------------------------------------


def test_the_same_input_always_yields_the_same_score():
    """工作单第二十六节"trend score deterministic"的直接断言。"""
    first = score_topic(cluster(), observed_at=NOW, claimed_sources=4)
    second = score_topic(cluster(), observed_at=NOW, claimed_sources=4)
    assert first == second
    assert first.reasons == second.reasons


def test_weights_sum_to_one():
    """权重和必须恰好是 1，否则满分不再是 100，而"85 分"就没有固定含义。"""
    assert sum(weight for _, weight in TREND_WEIGHTS) == pytest.approx(1.0)


def test_score_is_bounded():
    """分数恒在 0–100 之间——上限由封顶给出，下限由各分项非负给出。"""
    empty = score_topic([], observed_at=NOW, claimed_sources=0)
    assert empty.trend_score == 0.0
    best = score_topic(
        [post(f"ri-{i}", hours_ago=0.1, author=f"a{i}", likes=100000) for i in range(6)],
        observed_at=NOW,
        claimed_sources=6,
    )
    assert 0.0 <= best.trend_score <= 100.0


# --------------------------------------------------------------------------
# 分项口径：改一项输入，只有该变的那一项变
# --------------------------------------------------------------------------


def test_recency_halves_every_half_life():
    fresh = score_recency([post("a", hours_ago=0)], NOW)
    one_life = score_recency([post("a", hours_ago=24)], NOW)
    two_lives = score_recency([post("a", hours_ago=48)], NOW)
    assert fresh == 1.0
    assert one_life == pytest.approx(0.5, abs=1e-4)
    assert two_lives == pytest.approx(0.25, abs=1e-4)


def test_recency_uses_the_newest_post():
    """话题"现在还有人在说"取决于最新那条，不是最早那条。"""
    items = [post("old", hours_ago=60), post("new", hours_ago=1)]
    assert score_recency(items, NOW) == score_recency([items[1]], NOW)


def test_missing_publish_time_scores_zero_instead_of_guessing():
    """没有时间就记 0。把缺失当"刚刚"是编造，把它当"很久以前"也同样没有依据。"""
    undated = TopicScanItem(item_id="ri-x", post_id="x", author="ada", published_at=None)
    assert score_recency([undated], NOW) == 0.0
    assert score_novelty([undated], NOW) == 0.0


def test_spread_counts_distinct_authors_not_posts():
    """这是工作单第十节点名要防的那件事：一个大 V 连发多条不能算"扩散"。"""
    one_author = [post(f"ri-{i}", hours_ago=1, author="大V") for i in range(5)]
    assert score_spread(one_author) == pytest.approx(1 / SPREAD_FULL_AUTHORS, abs=1e-4)

    four_authors = [post(f"ri-{i}", hours_ago=1, author=f"u{i}") for i in range(4)]
    assert score_spread(four_authors) == 1.0


def test_unknown_author_is_not_counted_as_one_person():
    """空作者名是"不知道谁发的"，不是"同一个人"。算成一个人会让 spread 虚高。"""
    unknown = [post(f"ri-{i}", hours_ago=1, author="") for i in range(3)]
    assert score_spread(unknown) == 0.0


def test_engagement_weights_amplification_over_likes():
    """引用/转发是"选择放大"，比点赞重——权重写死在这里，改它必须是有意的。"""
    likes_only = post("a", hours_ago=1, likes=10)
    quotes_only = post("b", hours_ago=1, quotes=10)
    assert observed_engagement(quotes_only) > observed_engagement(likes_only)
    assert observed_engagement(post("c", hours_ago=1, likes=1, reposts=1, replies=1, quotes=1)) == 8


def test_engagement_saturates_instead_of_growing_forever():
    """取对数归一化的意义：500 与 50000 之间不该差 100 倍。"""
    modest = score_topic(
        [post("a", hours_ago=1, author="ada", likes=200)], observed_at=NOW, claimed_sources=1
    )
    viral = score_topic(
        [post("a", hours_ago=1, author="ada", likes=20000)], observed_at=NOW, claimed_sources=1
    )
    assert modest.engagement_score > 0.5
    assert viral.engagement_score == 1.0
    assert viral.engagement_score - modest.engagement_score < 0.5


def test_novelty_measures_position_in_the_window():
    """novelty 是"在当前观察窗口里排第几"，不是"这个词见没见过"。"""
    at_start = score_novelty([post("a", hours_ago=TREND_WINDOW_HOURS - 1)], NOW)
    midway = score_novelty([post("a", hours_ago=TREND_WINDOW_HOURS / 2)], NOW)
    at_end = score_novelty([post("a", hours_ago=0.5)], NOW)
    assert at_start < 0.05
    assert midway == pytest.approx(0.5, abs=0.02)
    assert at_end > 0.95


def test_novelty_uses_the_earliest_post():
    """"什么时候开始"取决于最早那条；用最新那条的话，任何话题都会显得是刚出现的。"""
    items = [post("first", hours_ago=70), post("last", hours_ago=1)]
    assert score_novelty(items, NOW) == score_novelty([items[0]], NOW)


def test_source_quality_is_penalised_by_unverifiable_citations():
    """编造出处要有代价：分母是"声称数"，不是"核验数"。"""
    assert score_source_quality(3, 3) == 1.0
    assert score_source_quality(1, 2) == 0.5
    assert score_source_quality(0, 3) == 0.0
    # 一条出处都没声称过，同样记 0：那意味着这个话题没有可追溯的支撑。
    assert score_source_quality(0, 0) == 0.0


# --------------------------------------------------------------------------
# 单一来源上限（工作单第十一 / 二十六节）
# --------------------------------------------------------------------------


def test_a_single_post_can_never_reach_a_high_score():
    """一条爆款帖子也不能单独制造高可信 trend。"""
    hot = [
        post("ri-1", hours_ago=0.2, author="大V", likes=50000, reposts=9000, replies=800, quotes=900)
    ]
    solo = score_topic(hot, observed_at=NOW, claimed_sources=1)
    assert solo.trend_score <= SINGLE_SOURCE_SCORE_CAP
    # 封顶前的分数留在 trend_score_raw 里——压过头却不留痕迹，
    # "为什么只有 40 分"就成了一个查不出来的问题。
    assert solo.trend_score_raw > SINGLE_SOURCE_SCORE_CAP
    assert any("单一来源上限" in reason for reason in solo.reasons)


def test_many_posts_from_one_author_are_still_single_source():
    """封顶看的是**不同作者数**，不是帖子条数：一个人连发十条仍然是一个人在说。"""
    thread = [post(f"ri-{i}", hours_ago=1, author="大V", likes=1000) for i in range(10)]
    capped = score_topic(thread, observed_at=NOW, claimed_sources=10)
    assert capped.trend_score <= SINGLE_SOURCE_SCORE_CAP
    assert capped.author_count == 1


def test_a_multi_author_cluster_can_exceed_the_cap():
    """对照组：门槛是结构性的，不是"所有话题都被压低"。"""
    spread = score_topic(cluster(), observed_at=NOW, claimed_sources=4)
    assert spread.trend_score > SINGLE_SOURCE_SCORE_CAP
    assert spread.trend_score_raw == spread.trend_score  # 没有触发封顶


# --------------------------------------------------------------------------
# 达标判定
# --------------------------------------------------------------------------


def test_a_healthy_cluster_is_qualified():
    result = score_topic(cluster(), observed_at=NOW, claimed_sources=4)
    qualification, lifecycle = qualify(
        score=result.trend_score,
        author_count=result.author_count,
        verified_sources=result.discussion_count,
        latest_seen_at=result.latest_seen_at,
        observed_at=NOW,
    )
    assert qualification is TopicQualification.QUALIFIED
    assert lifecycle is TopicLifecycle.QUALIFIED
    assert result.trend_score >= QUALIFIED_MIN_SCORE


def test_stale_is_decided_by_time_not_by_score():
    """工作单第二十六节："stale topic 不得被标记为 fresh"。

    一条很旧但很热闹的帖子：分数可以很高，判定仍然是 stale——
    时间过了就是过了，这是**事实**，不是打分结果。
    """
    old = NOW - timedelta(hours=STALE_AFTER_HOURS + 1)
    qualification, lifecycle = qualify(
        score=99.0, author_count=9, verified_sources=9, latest_seen_at=old, observed_at=NOW
    )
    assert qualification is TopicQualification.STALE
    assert lifecycle is TopicLifecycle.STALE


def test_missing_timestamps_are_treated_as_stale():
    """一条时间都不知道的帖子不能算"值得现在讨论"——我们没法证明它新。"""
    qualification, _ = qualify(
        score=99.0, author_count=9, verified_sources=9, latest_seen_at=None, observed_at=NOW
    )
    assert qualification is TopicQualification.STALE


def test_thin_support_is_beaten_by_time_and_structure_before_score():
    """判定顺序：时间 → 结构 → 分数。反过来先看分数，爆款会把前两条一起盖过去。"""
    fresh = NOW - timedelta(hours=1)
    assert qualify(
        score=99.0, author_count=1, verified_sources=5, latest_seen_at=fresh, observed_at=NOW
    ) == (TopicQualification.WEAK_SUPPORT, TopicLifecycle.DISCARDED)
    assert qualify(
        score=99.0, author_count=5, verified_sources=1, latest_seen_at=fresh, observed_at=NOW
    ) == (TopicQualification.WEAK_SUPPORT, TopicLifecycle.DISCARDED)
    # 结构够了但分数不够：它还在候选里，只是不值得优先研究。
    assert qualify(
        score=QUALIFIED_MIN_SCORE - 1,
        author_count=3,
        verified_sources=3,
        latest_seen_at=fresh,
        observed_at=NOW,
    ) == (TopicQualification.LOW_SIGNAL, TopicLifecycle.DISCOVERED)


def test_low_score_keeps_the_topic_in_the_candidate_list():
    """不达标 ≠ 不存在。候选列表要给 Planner 看全部，只是它有分档可依。"""
    _, lifecycle = qualify(
        score=1.0, author_count=4, verified_sources=4, latest_seen_at=NOW, observed_at=NOW
    )
    assert lifecycle is TopicLifecycle.DISCOVERED


# --------------------------------------------------------------------------
# 排序
# --------------------------------------------------------------------------


def test_ranking_is_deterministic_even_on_a_tie():
    """同分同条数时按标题收尾：LLM 给出的簇顺序对同一批材料并不稳定，
    而"并列时谁在前面"如果随它变，同一份输入就会排出两个顺序。"""
    a = post("ri-1", hours_ago=1, author="ada", likes=7)
    b = post("ri-2", hours_ago=1, author="bob", likes=7)
    first = score_topic([a], observed_at=NOW, claimed_sources=1)
    second = score_topic([b], observed_at=NOW, claimed_sources=1)
    assert first.trend_score == second.trend_score

    # 分数与条数都并列时，标题把两者区分开——于是并列不再是"顺序未定义"，
    # 而是"顺序由标题定"。没有这一项，两份分数相同的候选谁在前就取决于
    # 它们碰巧从哪个列表里来。
    assert rank_key(first, "甲")[:2] == rank_key(second, "乙")[:2]
    assert rank_key(first, "甲") != rank_key(second, "乙")
    # 而同一份输入重算一次，键完全一致。
    assert rank_key(first, "甲") == rank_key(
        score_topic([a], observed_at=NOW, claimed_sources=1), "甲"
    )

    higher = score_topic(
        [post("ri-3", hours_ago=1, author="ada", likes=7), post("ri-4", hours_ago=1, author="bob")],
        observed_at=NOW,
        claimed_sources=2,
    )
    assert rank_key(higher, "丙") < rank_key(first, "甲")  # 分高的排前面
