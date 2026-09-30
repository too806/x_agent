"""`app/style/corpus.py`：一条帖子够不够格进 Style Corpus。

工作单第十一、十二节把两条写成硬要求，这里逐条钉住：

- 筛选是**组合**条件，阈值全部来自配置（不硬编码）；
- **互动量不是质量真值**——它只是一个粗筛，过不了门槛的样本照样要
  留下理由，因为"为什么这批样本这么少"必须能被回答。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.style.config import AccountConfig, CorpusConfig, StyleConfig
from app.style.corpus import (
    CandidateSample,
    collect_slices,
    collection_query,
    collection_window,
    engagement_score,
    evaluate_sample,
    filter_candidates,
    is_link_only,
)

CFG = CorpusConfig()

#: 一条长度合法（>min_length=40）的正文。
BODY = "先把结论放在前面：模型变小不是重点，调度才是。多数团队把预算花在了错的地方，这一条值得单独说清楚。"


def candidate(**overrides) -> CandidateSample:
    base = dict(
        platform_post_id="p1",
        author_username="someone",
        content=BODY,
        likes=10,
        reposts=5,
        replies=0,
        quote_count=0,
        is_repost=False,
    )
    base.update(overrides)
    return CandidateSample(**base)


# ---------------------------------------------------------------------------
# 纯链接
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["看这个 https://t.co/abcdefg", "https://example.com/a/b/c", "www.example.com/x"],
)
def test_link_only_posts_are_recognised(content):
    """去掉链接后没有实质内容，学不到写法。"""
    assert is_link_only(content) is True


def test_a_post_with_a_link_and_real_text_is_not_link_only():
    assert is_link_only("这一版的重点不在参数，而在调度：https://t.co/abcdefg") is False


# ---------------------------------------------------------------------------
# 组合筛选
# ---------------------------------------------------------------------------


def test_a_clean_post_is_included():
    decision = evaluate_sample(candidate(), CFG)
    assert decision.included is True
    # 收进来的样本没有理由要写——编一句"因为合格"只会让日志多一行噪声。
    assert decision.reason is None


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"content": ""}, "empty_content"),
        ({"content": "   "}, "empty_content"),
        ({"content": "看这个 https://t.co/x"}, "link_only"),
        ({"is_repost": True}, "repost"),
        ({"is_reply": True}, "reply"),
        ({"content": "太短了"}, f"too_short(<{CFG.min_length})"),
        ({"content": "长" * (CFG.max_length + 1)}, f"too_long(>{CFG.max_length})"),
        ({"content": BODY + "抽奖转发本条"}, "promo(抽奖)"),
        ({"likes": 0}, f"low_likes(<{CFG.min_likes})"),
        (
            {"likes": 5, "reposts": 0, "replies": 0, "quote_count": 0},
            f"low_engagement(<{CFG.min_engagement_score})",
        ),
    ],
)
def test_each_rule_excludes_the_post(overrides, expected):
    decision = evaluate_sample(candidate(**overrides), CFG)
    assert decision.included is False
    assert decision.reason == expected


def test_excluded_samples_always_carry_a_reason():
    """被排除却不说为什么，等于让"语料为什么这么少"永远无法回答。"""
    for overrides in [{"content": ""}, {"is_repost": True}, {"likes": 0}, {"content": "短"}]:
        decision = evaluate_sample(candidate(**overrides), CFG)
        assert decision.included is False
        assert decision.reason


def test_the_reason_is_the_specific_one_not_a_generic_verdict():
    """短路顺序让理由总是**最具体**的那一条。

    一条既转发、又太短、又没人点赞的帖子，理由是 `repost`——采集的人
    据此知道"这条被剔是因为它不是我写的"，而不是一句"综合不合格"。
    """
    decision = evaluate_sample(candidate(is_repost=True, content="短", likes=0), CFG)
    assert decision.reason == "repost"


def test_cheap_checks_run_before_expensive_ones():
    """长度判定在互动折算之前：太短的帖子不必先去算加权分。"""
    decision = evaluate_sample(candidate(content="短", likes=999, reposts=999), CFG)
    assert decision.reason.startswith("too_short")


# ---------------------------------------------------------------------------
# 阈值来自配置，不硬编码
# ---------------------------------------------------------------------------


def test_thresholds_come_from_the_config_object():
    """同一份候选，换一套阈值得到不同结论——证明阈值真的被读，没有写死。"""
    strict = CorpusConfig(min_likes=100)
    loose = CorpusConfig(min_likes=1, min_engagement_score=1)
    assert evaluate_sample(candidate(likes=10, reposts=0), strict).included is False
    assert evaluate_sample(candidate(likes=10, reposts=0), loose).included is True


def test_a_corpus_config_rejects_an_impossible_length_range():
    """`min_length >= max_length` 会让一条样本都收不进来，而那离原因很远。"""
    with pytest.raises(ValueError, match="min_length"):
        CorpusConfig(min_length=100, max_length=100)


def test_reposts_can_be_kept_when_the_config_says_so():
    """排除转发是默认，不是写死的：配置改成 false 就得能收。"""
    decision = evaluate_sample(candidate(is_repost=True), CorpusConfig(exclude_reposts=False))
    assert decision.included is True


def test_a_reply_is_never_reported_as_a_repost():
    """回复与转发**必须**是两个理由。

    X 的 `referenced_tweets` 是一个混装列表（retweeted / quoted /
    replied_to）。只看"非空"的话，一条回复会带着 `repost` 这个理由被剔掉
    ——而"理由总是最具体的那一条"正是短路存在的意义。理由错了之后，
    "这批语料为什么这么少"的答案也就跟着错了。
    """
    decision = evaluate_sample(candidate(is_reply=True), CFG)
    assert decision.reason == "reply"

    both = evaluate_sample(candidate(is_reply=True, is_repost=True), CFG)
    assert both.reason == "repost", "两者同时成立时，转发是更靠前的那条规则"


def test_replies_can_be_kept_when_the_config_says_so():
    """回复也是配置项，不是写死的。"""
    config = CorpusConfig(exclude_replies=False)
    assert evaluate_sample(candidate(is_reply=True), config).included is True
    # 而且关掉它**不会**顺手把转发也放开——两个开关互不影响。
    assert evaluate_sample(candidate(is_repost=True), config).reason == "repost"


def test_the_reply_switch_is_independent_of_the_repost_switch():
    only_replies_off = CorpusConfig(exclude_replies=False, exclude_reposts=True)
    only_reposts_off = CorpusConfig(exclude_replies=True, exclude_reposts=False)
    assert evaluate_sample(candidate(is_reply=True), only_replies_off).included is True
    assert evaluate_sample(candidate(is_repost=True), only_replies_off).reason == "repost"
    assert evaluate_sample(candidate(is_repost=True), only_reposts_off).included is True
    assert evaluate_sample(candidate(is_reply=True), only_reposts_off).reason == "reply"


# ---------------------------------------------------------------------------
# 互动量：只是门槛，不是质量
# ---------------------------------------------------------------------------


def test_engagement_score_weights_quotes_highest():
    """引用权重最高：它是"这条内容被当作论据引用"的信号，与点赞不是一回事。"""
    likes = engagement_score(candidate(likes=1, reposts=0, replies=0, quote_count=0), CFG.engagement_weights)
    quotes = engagement_score(candidate(likes=0, reposts=0, replies=0, quote_count=1), CFG.engagement_weights)
    assert quotes > likes


def test_high_engagement_does_not_by_itself_guarantee_inclusion():
    """互动再高，正文太短（学不到结构）照样不收。

    这条是工作单第十二节"高互动 ≠ 适合学习"的可执行版本。
    """
    decision = evaluate_sample(candidate(content="好", likes=10**6, reposts=10**6), CFG)
    assert decision.included is False
    assert decision.reason.startswith("too_short")


# ---------------------------------------------------------------------------
# 批量筛选：保留被排除的
# ---------------------------------------------------------------------------


def test_filter_candidates_keeps_the_excluded_ones_with_reasons():
    """被排除的必须留在结果里（连同理由），调用方要写库的正是这两部分。"""
    results = filter_candidates([candidate(), candidate(platform_post_id="p2", is_repost=True)], CFG)
    assert [decision.included for _, decision in results] == [True, False]
    assert results[1][1].reason == "repost"


# ---------------------------------------------------------------------------
# 采集窗口与查询词
# ---------------------------------------------------------------------------


def test_collection_query_never_doubles_the_at_sign():
    assert collection_query("someone") == "from:someone"
    assert collection_query("@someone") == "from:someone"


def test_collection_window_is_a_closed_range_ending_now():
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    start, end = collection_window(now, 6)
    assert end == now
    assert end - start == timedelta(days=6)


def test_collection_window_is_independent_of_the_trend_window():
    """语料窗口刻意**不**复用 trend 的 72 小时。

    72 小时回答"为什么这条讨论是现在值得写的"，语料窗口回答"这些人平时
    怎么写"。混成一个口径会让两边都失去意义，所以窗口长度是显式传入的。
    """
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert collection_window(now, 6)[0] == now - timedelta(days=6)
    assert collection_window(now, 3)[0] == now - timedelta(days=3)


# ---------------------------------------------------------------------------
# 窗口切片
#
# 一次请求最多 100 条且不翻页，所以"切不切"直接决定报告里的"取回 N 条"
# 到底是"窗口里真有 N 条"还是"我们只读了最新的一页"。
# ---------------------------------------------------------------------------


def test_a_single_slice_is_the_window_itself():
    """`slices=1` 必须与不切片**逐位相同**——这是它敢当默认值的理由。"""
    start = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert collect_slices(start, end, 1) == [(start, end)]
    # 非正数也按"不切"处理：返回空列表会让采集**静默地一条都不问**。
    assert collect_slices(start, end, 0) == [(start, end)]


def test_slices_are_contiguous_and_cover_the_window_exactly_once():
    """首尾相接、无空隙、无重叠：窗口里每一秒都恰好落在一段里。"""
    start = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
    slices = collect_slices(start, end, 6)
    assert len(slices) == 6
    assert slices[0][0] == start, "第一段必须从窗口起点开始"
    assert slices[-1][1] == end, "最后一段必须到窗口终点为止"
    for (_, previous_end), (next_start, _) in zip(slices, slices[1:]):
        assert previous_end == next_start, "前一段的终点必须就是后一段的起点"


def test_more_slices_do_not_move_the_window():
    """切分只改变"问几次"，不改变"看的是哪一段时间"。

    这是它与"把窗口开大"的**本质区别**：后者会改变语料的时间覆盖，
    让 Profile 混进别的时段；前者只是把同一段时间读完。
    """
    start = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
    for slices in (2, 3, 6, 24):
        edges = collect_slices(start, end, slices)
        assert edges[0][0] == start
        assert edges[-1][1] == end


def test_slices_stay_inside_the_window():
    """任何一段都不得越过窗口边界——否则采到窗口外的帖子，
    而 Profile 的 `sample_window` 会与样本对不上。"""
    start = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
    for slice_start, slice_end in collect_slices(start, end, 5):
        assert start <= slice_start < slice_end <= end


# ---------------------------------------------------------------------------
# 采样账号的配置（工作单第七、八、三十节）
#
# 这些校验的价值全在"错了会静默"上：一个拼错的分类不会让任何东西报错，
# 它只是让"按分类统计"失去意义，而语料已经采完了。
# ---------------------------------------------------------------------------


def test_account_username_accepts_the_at_sign_people_actually_type():
    assert AccountConfig(username="@someone").username == "someone"
    assert AccountConfig(username="  someone  ").username == "someone"


def test_an_empty_username_is_rejected():
    with pytest.raises(ValidationError):
        AccountConfig(username="@")
    with pytest.raises(ValidationError):
        AccountConfig(username="   ")


def test_an_unknown_category_is_rejected_rather_than_stored():
    with pytest.raises(ValidationError) as info:
        AccountConfig(username="someone", category="LLM")
    # 错误信息要能指出**可选值**，否则人得去翻源码才知道该怎么改。
    assert "大模型" in str(info.value)


def test_a_duplicate_account_is_rejected_rather_than_deduped():
    """同名两行时"这个账号到底采不采"取决于处理顺序——那是个无解的问题。"""
    with pytest.raises(ValidationError) as info:
        StyleConfig(
            accounts=[
                AccountConfig(username="someone", enabled=True),
                AccountConfig(username="@someone", enabled=False),
            ]
        )
    assert "someone" in str(info.value)


def test_accounts_default_to_empty_so_nothing_is_collected_by_default():
    """空配置 = 一个账号都不采。

    这一条是刻意的：**没有默认采样范围**。工作单第三十节要求账号集合由人
    指定；给一份"内置的推荐账号"会让"我们学了谁"变成代码决定的事。
    """
    assert StyleConfig().accounts == []
    assert AccountConfig(username="someone").source_type == "manual"
