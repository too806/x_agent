"""`app/style/features.py` 的确定性原语。

这一层是整个 5B.2 的地基：Profile 的每个数字都从这些函数来，而 Profile 的可信
理由只有一个——**同一段文本永远算出同一个数**。所以这里测得最密的是两件事：

1. 复现性（同输入同输出，与遍历顺序、挂钟时间无关）；
2. "算不出来"必须是 `None`，不能是 `0.0`（`0/0` 被写成 0，会让下游把
   "没测" 读成 "测出来是零"，而这两个结论在聚合时差别是全部）。
"""

from __future__ import annotations

import pytest

from app.style import features


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------


def test_split_sentences_consumes_the_terminator():
    """`split_sentences` 给的是**内容**：标点是分隔符，不属于返回的句子。

    这条同时是 `sentences_with_terminator` 存在的理由——需要标点的调用方
    不能用这个函数，否则 `endswith("?")` 永远为假。
    """
    assert features.split_sentences("第一句。第二句！第三句？") == ["第一句", "第二句", "第三句"]


def test_sentences_with_terminator_keeps_it():
    assert features.sentences_with_terminator("第一句。第二句！") == ["第一句。", "第二句！"]


def test_split_paragraphs_treats_a_single_newline_as_layout():
    """单换行不算分段。短帖里它常常只是排版，算成段会让段落数虚高。"""
    assert features.split_paragraphs("第一段\n第二行\n\n第二段") == ["第一段\n第二行", "第二段"]


def test_split_paragraphs_of_plain_text_is_one_paragraph():
    """没有空行但正文非空 == 一段，不是零段。"""
    assert features.split_paragraphs("就一段话") == ["就一段话"]
    assert features.split_paragraphs("   ") == []


# ---------------------------------------------------------------------------
# 分位数：算不出来是 None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("values", [[], ()])
def test_percentile_of_nothing_is_none(values):
    assert features.percentile(values, 50) is None
    assert features.median(values) is None
    assert features.mean_or_none(values) is None


def test_percentile_interpolates_linearly():
    assert features.percentile([1, 2, 3, 4], 25) == pytest.approx(1.75)
    assert features.median([1, 2, 3, 4]) == pytest.approx(2.5)
    assert features.percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)


def test_percentile_of_one_value_is_that_value():
    """corpus 里只采到一条样本是完全可能的——那时要一个数，不是一个异常。

    这正是这里不用 `statistics.quantiles`（要求至少两个数据点）的原因。
    """
    assert features.percentile([7], 25) == 7.0
    assert features.median([7]) == 7.0


def test_percentile_is_order_independent():
    values = [9, 1, 5, 3, 7]
    assert features.percentile(values, 25) == features.percentile(sorted(values), 25)


# ---------------------------------------------------------------------------
# 密度：分母为 0 一律 None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "measure",
    [
        features.emoji_density,
        features.question_density,
        features.number_density,
        features.english_ratio,
        features.abstract_noun_density,
    ],
)
def test_density_of_empty_text_is_none_not_zero(measure):
    """空文本的 "每百字几个 X" 不存在，不是 0。

    这条若退化成 0.0，Profile 里就会出现一批"测出来是零"的假数据，
    而它们与"没测"在报告里长得一模一样。
    """
    assert measure("") is None
    assert measure("   \n\t ") is None


def test_question_density_counts_both_marks():
    assert features.count_questions("为什么？真的?") == 2


def test_number_density_counts_tokens_not_digits():
    """`1000` 是一个数字 token，不是四个。按 token 计数才与"信息密度"的直觉一致。"""
    assert features.count_numbers("增长 1000%，从 3.5 到 4") == 3


def test_emoji_count_matches_a_known_emoji():
    assert features.emoji_count("很好 🎉") == 1
    assert features.emoji_density("🎉🎉🎉🎉") is not None


def test_english_ratio_is_bounded_and_ignores_whitespace():
    ratio = features.english_ratio("GPT-5 很强")
    assert ratio is not None
    assert 0.0 < ratio <= 1.0
    # 空白不进分母
    assert features.english_ratio("ab") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 计数与实体
# ---------------------------------------------------------------------------


def test_find_generic_phrases_is_case_insensitive():
    """英文词表用小写写，而正文里可能是 `In Today's`。"""
    assert "in today's" in features.find_generic_phrases("In Today's world")
    assert "值得注意的是" in features.find_generic_phrases("值得注意的是，这件事")


def test_count_hedges_counts_every_occurrence():
    assert features.count_hedges("可能吧，也许。") == 2


def test_find_concrete_entities_dedupes():
    """同一个指称出现五次，具体性不因此翻五倍——它仍然是一个具体指称。"""
    entities = features.find_concrete_entities("GPT-5 比 GPT-5 强")
    assert entities.count("GPT-5") == 1


def test_find_concrete_entities_drops_english_function_words():
    """`the` 不是具体实体。宁可少算几个，也不要把功能词算成"具体"。"""
    assert "the" not in features.find_concrete_entities("the model is here")


def test_repeated_openers_counts_only_the_repeats():
    """超过一次才算重复：单条组贡献 0，两条的组贡献 2-1=1。

    取的是**前 4 个字符**，所以重复必须落在同一前缀上——
    "首先看这个" 与 "首先看那个" 在第 4 字就分开了，不算重复句式。
    """
    sentences = ["首先看这个", "首先看这个也行", "另外一件事"]
    assert features.repeated_openers(sentences) == 1
    assert features.repeated_openers(["首先看这个", "首先看那个"]) == 0


def test_find_repeated_patterns_needs_a_third_occurrence():
    """出现两次可能是修辞，三次才是套路。"""
    twice = "AI 不是魔法，AI 不是魔法"
    assert features.find_repeated_patterns(twice) == []
    thrice = "AI 不是魔法。AI 不是魔法。AI 不是魔法。"
    assert features.find_repeated_patterns(thrice)


# ---------------------------------------------------------------------------
# 分布
# ---------------------------------------------------------------------------


def test_distribution_returns_shares_sorted_by_label():
    assert features.distribution(["b", "a", "b", "a"]) == {"a": 0.5, "b": 0.5}


def test_distribution_of_nothing_is_empty_dict():
    assert features.distribution([]) == {}
    assert features.distribution(["", ""]) == {}


def test_distribution_sums_to_one():
    shares = features.distribution(["x", "y", "z", "x"])
    assert sum(shares.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 表层形态
#
# 这里的每一条都对应一个**曾经静默失效**的分支：早期实现用
# `split_sentences`（它把标点切掉了）再判 `endswith("?")`，于是 question
# 开头/结尾永远统计不出来，Profile 会如实写下一句"这个语料里没有问句"——
# 而那是假的。所以下面把五种开头、三种结尾逐个钉住。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("为什么要卷上下文窗口？因为瓶颈不在那里。", "question"),
        ("2024 年，成本结构变了。", "number"),
        ("「AI 不是魔法」，这句话只对了一半。", "quote"),
        ("各位，聊聊这个。", "address"),
        ("GPT-5 的推理成本降了 90%。", "statement"),
    ],
)
def test_opening_pattern_recognises_every_surface_form(text, expected):
    assert features.opening_pattern(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("事情就是这样。你会怎么选？", "question"),
        ("欢迎在评论区说说你的用法。", "call"),
        ("不妨自己试一次。", "call"),
        ("事情就是这样。", "statement"),
    ],
)
def test_closing_pattern_recognises_every_surface_form(text, expected):
    assert features.closing_pattern(text) == expected


def test_patterns_of_empty_text_are_statement():
    """没有正文时给一个中性的形态，而不是抛异常或返回 None。

    下游 `distribution()` 只认字符串标签；返回 None 会让它把这条样本
    悄悄丢掉，于是分布的分母与实际样本数对不上。
    """
    assert features.opening_pattern("") == "statement"
    assert features.closing_pattern("") == "statement"


# ---------------------------------------------------------------------------
# 复现性
# ---------------------------------------------------------------------------


def test_every_measure_is_a_pure_function_of_the_text():
    """同一段文本算两遍必须逐位相同。

    Profile 的可复现性（Smoke 5）完全建立在这条上：这里只要有一个函数
    依赖了集合遍历顺序或挂钟时间，Profile 就会开始漂。
    """
    text = "2024 年，GPT-5 的成本降了 90% 🎉。值得注意的不是数字，是谁受益。"
    measures = [
        features.split_sentences,
        features.split_paragraphs,
        features.sentences_with_terminator,
        features.find_generic_phrases,
        features.count_hedges,
        features.count_numbers,
        features.count_questions,
        features.find_concrete_entities,
        features.find_repeated_patterns,
        features.emoji_count,
        features.emoji_density,
        features.question_density,
        features.number_density,
        features.english_ratio,
        features.opening_pattern,
        features.closing_pattern,
    ]
    for measure in measures:
        assert measure(text) == measure(text), measure.__name__
