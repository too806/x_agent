"""跨帖重复检测：三层各自的**命中**与**不命中**。

这是自动发布里最要紧的一道闸门——无人值守意味着没人会在发出去之前发现
一条重复内容，而 X 把重复/实质相似的内容明确定性为 spam。所以这里不只测
"能抓到重复"，更要测**绕过尝试**（换行拆散、加空格、改大小写）与
**合理的不命中**（不相干的短稿不该被拦住——误拦会让人把闸门关掉）。

第三类测试是 lexical 层：它**只记录、不拦截**。那不是保守，是它测不准
（见 app/style/repetition.py 的说明）。把它写成拦截是很容易犯的错，
而且犯错的代价是"闸门老是误拦"，所以这里明确钉住它拦不下任何东西。
"""

from app.style.repetition import (
    LAYER_TOPIC,
    LAYER_VERBATIM,
    RepetitionResult,
    compute_lexical_containment,
    detect_topic_recency,
    detect_verbatim,
    evaluate_repetition,
    normalize_topic,
)
from app.style.spans import SHARED_SPAN

#: 一段远长于 SHARED_SPAN 的正文，用作"已经写过的东西"。
OLDER = (
    "大模型的推理成本在过去一年下降了十倍，这让小团队第一次用得起前沿能力。"
    "但便宜的推理也意味着更多同质化的内容，注意力会变成更稀缺的资源。"
)

#: 与 OLDER 共享一段连续原文的新稿（换了个开头和结尾）。
REPHRASED = (
    "最近在观察一件事：大模型的推理成本在过去一年下降了十倍，"
    "于是真正的问题变成了谁能把便宜算力换成独有的判断。"
)


def test_verbatim_catches_shared_span():
    hits = detect_verbatim(REPHRASED, [OLDER])

    assert len(hits) == 1
    assert hits[0].layer == LAYER_VERBATIM
    # evidence 必须是命中的**原文**，而不是一个布尔或计数——人看到拦下的理由时
    # 要能一眼确认"真的是这一段"（沿用 GateViolation.detail 的做法）。
    assert hits[0].evidence
    assert hits[0].evidence in "".join(OLDER.split())


def test_verbatim_survives_whitespace_rearrangement():
    """把抄来的那段按行拆开重排——这是最省事的绕过方式，必须无效。

    `spans.py` 在切片前先去掉空白，正是为了挡这个。这条测试守的是那个性质
    不被"优化掉"：把去空白改掉之后，下面这个 variant 会立刻漏过去。
    """
    copied = "大模型的推理成本在过去一年下降了十倍"
    variant = copied[:6] + "\n   " + copied[6:12] + " \n" + copied[12:]

    hits = detect_verbatim(variant, [OLDER])
    assert len(hits) == 1


def test_verbatim_ignores_short_overlap():
    """短于 SHARED_SPAN 的重合不是重复。

    中文里"大模型""这件事"这类搭配到处都是，把窗口调小会让误报爆炸。
    """
    short = "大模型的推理成本很高。"  # 与 OLDER 的重合远不到 SHARED_SPAN
    assert SHARED_SPAN > len("大模型的推理")
    assert detect_verbatim(short, [OLDER]) == []


def test_verbatim_empty_inputs():
    assert detect_verbatim("", [OLDER]) == []
    assert detect_verbatim(OLDER, []) == []
    assert detect_verbatim("随便写点什么完全不相干的话。", [OLDER]) == []


def test_topic_recency_matches_after_normalization():
    """题目比较前要归一化：大小写与多余空白都不该造成"这是新题目"的假象。"""
    # 大小写不同（A/a）+ 空白数量不同（两个空格 / 一个空格），
    # 归一化之后是同一个题目。空白只折叠**连续的**那些，不改变位置——
    # 折成"a 模型的推理成本"与"a 模型的推理成本"才是同一个串。
    recent = ["A  模型的推理成本"]
    assert detect_topic_recency("a 模型的推理成本", recent)[0].layer == LAYER_TOPIC


def test_topic_recency_is_exact_not_fuzzy():
    """刻意不做模糊匹配。

    题目串短，模糊匹配会把"同一个模型的两个不同角度"判成重复，而这类误拦
    会累积成"闸门老是误拦"的印象，最终让人把闸门关掉。
    """
    assert detect_topic_recency("A 模型的推理成本", ["A 模型的推理成本与路由"]) == []


def test_topic_recency_ignores_empty_topic():
    assert detect_topic_recency(None, ["随便什么"]) == []
    assert detect_topic_recency("   ", ["随便什么"]) == []


def test_normalize_topic_only_collapses_whitespace_and_case():
    assert normalize_topic("  A   B  ") == "a b"
    # 标点**不**去掉：再往下走就开始制造"看起来一样、其实是两个题目"的假命中。
    assert normalize_topic("A，B") != normalize_topic("A B")


def test_lexical_containment_is_reported_for_long_enough_text():
    """lexical 层确实会算出数——它不是永远 None。

    没有这条的话，下面那条"lexical 不拦人"会因为 lexical 根本没跑而恒真。
    """
    corpus = OLDER
    content = OLDER + "，另外补充一点别的。"

    ratio, nearest = compute_lexical_containment(content, [corpus])
    assert ratio is not None and ratio > 0
    assert nearest == corpus[:60]


def test_lexical_alone_does_not_block():
    """只有 lexical 有话说时，判据必须**不拦**。

    构造：新稿与语料共享一段 10 字的连续原文——长于 lexical 的 n=8（所以
    lexical 会命中），短于 verbatim 的 SHARED_SPAN=12（所以 a 层干净）。
    这正是"同一个意思换个说法"的近似形态：字符层面很像，但没有整段搬运。

    把它写成拦截是很容易犯的错（"相似度超过阈值就拒"），而代价是闸门老是误拦，
    最终让人把闸门整个关掉——那比没有闸门更糟。
    """
    run = "乙" * 10
    assert 8 <= len(run) < SHARED_SPAN
    corpus = "甲" * 20 + run + "丙" * 20
    content = "丁" * 20 + run + "戊" * 20
    assert len(content) >= 40  # 短于此长度的正文 lexical 根本不算

    result = evaluate_repetition(content=content, topic="全新题目", recent_contents=[corpus])

    assert result.lexical_containment is not None and result.lexical_containment > 0
    assert result.hits == ()
    assert result.blocks is False


def test_blocks_is_driven_only_by_hits():
    """`blocks` 的定义就是 `bool(hits)`，与 lexical 的取值无关。

    直接构造结果对象：这条是 `RepetitionResult` 自己的性质，
    而构造一个"lexical 很高、a/c 全清"的真实输入要精心摆字符，
    那样测的是构造技巧而不是这条性质。
    """
    assert RepetitionResult(hits=(), lexical_containment=0.99).blocks is False


def test_lexical_is_none_when_nothing_to_compare():
    """没有可比对象 → `None`，**不是 0**。

    "没有语料可比"和"完全不像"在下游是两件事（同 5C 的 NULL ≠ 0）。
    """
    assert compute_lexical_containment("随便一句话", []) == (None, None)
    assert compute_lexical_containment("短", ["随便一句长一些的话"]) == (None, None)


def test_evaluate_repetition_is_deterministic():
    args = dict(content=REPHRASED, topic="A 模型", recent_contents=[OLDER], recent_topics=["B"])
    first = evaluate_repetition(**args)
    second = evaluate_repetition(**args)

    assert first == second


def test_clean_content_produces_no_hits():
    """全新的题目 + 全新的措辞 → 一层都不该命中。

    这条是"闸门不会平白无故拦人"的最小保证。
    """
    result = evaluate_repetition(
        content="完全不相干的一段话，讲的是别的事情，用词也没有重叠。",
        topic="一个从没用过的题目",
        recent_contents=[OLDER],
        recent_topics=["A 模型的推理成本"],
    )
    assert result.hits == ()
    assert result.blocks is False
