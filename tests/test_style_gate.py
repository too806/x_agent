"""发布内容门禁：内部编号泄漏与低质点击诱饵。

**这是安全测试，不是风格测试。** 每误判一格，代价不是"界面不好看"，是两条中的一条：

- **漏判** → 一条带着 `（f4）` 的正文被发到真实 X 账号上，读者看到一串内部编号；
- **误判** → 一条合格的正文被 422 拦下，发不出去，而人得对着一条看不懂的理由找原因。

所以这里分三层测：

1. `find_internal_refs` 的**正反例表**——反例表里全是真实存在的"f+数字"，
   `F-16`、`F1 赛车`、`RTX5090`、`Wi-Fi 6`……它们**一个都不许**被判成泄漏。
2. 裸形编号的**交叉核对**：同一个 `f18`，本轮证据里有就是泄漏，没有就不是。
3. 门禁与 guardrail 的接线：干净内容一个原因码都不多——guardrail 的既有测试
   断言的是**精确列表**，多一个码就会连锁红掉一批无关用例。
"""

import pytest

from app.agent.nodes.guardrail import evaluate
from app.style.features import (
    CLICKBAIT_PHRASES,
    find_clickbait,
    find_internal_refs,
)
from app.style.gate import (
    REASON_CLICKBAIT,
    REASON_INTERNAL_REF,
    evaluate_content_gate,
)

VALID = "AI coding agents ship faster."


# ---------------------------------------------------------------------------
# 1. 内部编号：括号形（零假阳性，总能算）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("他没说清楚（f4）。", ["f4"]),
        ("他没说清楚(f4)。", ["f4"]),
        ("见 [f12] 那段。", ["f12"]),
        ("按照【f7】的说法。", ["f7"]),
        ("大写也要抓（F9）。", ["f9"]),  # 归一化成小写
        ("f18只说加速进入，f19说…", []),  # 没有括号、也没有 fact_ids → 不算
        ("（f4）和（f7）都提到了", ["f4", "f7"]),
        ("重复（f4）（f4）", ["f4"]),  # 去重
    ],
)
def test_bracketed_refs_are_always_caught(text, expected):
    assert find_internal_refs(text) == expected


def test_bracketed_frame_has_zero_false_positives():
    """括号里出现"f+数字"在正常中文里不是一种写法，所以这一支不需要核对。"""
    assert find_internal_refs("（f4）") == ["f4"]
    # 没有 fact_ids 也照样算。
    assert find_internal_refs("（f4）", fact_ids=None) == ["f4"]


# ---------------------------------------------------------------------------
# 2. 内部编号：裸形（必须交叉核对）
# ---------------------------------------------------------------------------


def test_bare_ref_counts_only_when_it_is_a_real_fact_id():
    text = "f18只说加速进入，没有解释责任划分。"
    assert find_internal_refs(text, fact_ids=["f18"]) == ["f18"]
    # 编号对不上本轮证据 → 不是泄漏（它可能是别的东西）。
    assert find_internal_refs(text, fact_ids=["f1", "f2"]) == []


def test_bare_ref_is_disabled_without_fact_ids():
    """V1 链路没有 evidence_pack，也就没有可核对的编号——此时保守起见不启用裸形。

    宁可漏掉一个裸形编号，也不要拦住一条把 `F-16` 当事实写进去的正常稿子。
    """
    assert find_internal_refs("f18只说加速进入", fact_ids=None) == []


@pytest.mark.parametrize(
    "text",
    [
        "F-16 战机加入了这次演习。",
        "F1 赛车的排位赛结果出来了。",
        "RTX5090 的功耗又涨了。",
        "GPT-4 之后，节奏明显变了。",
        "H100 显卡还是拿不到货。",
        "F-35 的单价又创了新高。",
        "B2 轰炸机出动了一次。",
        "iPhone 15 换了接口。",
        "M2 芯片的性能提升有限。",
        "USB-C 终于统一了。",
        "H-1B 抽签结果出来了。",
        "Wi-Fi 6 的覆盖还是不够。",
        "A4 纸大小的原型机。",
        "Model 3 又降价了。",
        "f16 战机低空掠过。",  # 小写 + 中文相邻，但没有对应的 fact_id
        "f12 控制台里什么都没显示。",
    ],
)
def test_bare_ref_does_not_flag_real_world_f_tokens(text):
    """这些是真实存在的"f+数字"指称，一个字都不许判成泄漏。"""
    assert find_internal_refs(text) == []
    # 即便给了 fact_ids，只要不是这一条，也照样不判。
    assert find_internal_refs(text, fact_ids=["f1", "f2"]) == []


# ---------------------------------------------------------------------------
# 3. 点击诱饵：窄，是这张表唯一的价值
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", CLICKBAIT_PHRASES)
def test_every_clickbait_phrase_is_found(phrase):
    assert phrase in find_clickbait(f"前面的话 {phrase} 后面的话")


@pytest.mark.parametrize(
    "text",
    [
        "没想到这么快就落地了。",
        "居然真的做成了。",
        "真相可能没那么简单。",
        "值得注意的是，这次不一样。",
        "问题可能没这么简单。",
        "真正麻烦的不是模型，是流程。",
    ],
)
def test_tension_words_are_not_clickbait(text):
    """这些是阅读契约**明确允许**的张力词。收进来会误伤合格的稿子。"""
    assert find_clickbait(text) == []


# ---------------------------------------------------------------------------
# 4. 门禁：干净放行、脏拦截
# ---------------------------------------------------------------------------


def test_clean_content_produces_no_violation():
    assert evaluate_content_gate(VALID) == []
    assert evaluate_content_gate("") == []
    assert evaluate_content_gate(None) == []
    # 有张力、有绝对句式，但事实站得住 → 不是门禁的事。
    assert evaluate_content_gate("别再指望沙箱兜底了。这不是安全加固，是承认范式已被证伪。") == []


def test_ref_leak_is_reported_with_the_offending_id():
    violations = evaluate_content_gate("他没说清楚（f4），也没解释。")
    assert [v.code for v in violations] == [REASON_INTERNAL_REF]
    # 理由里必须带上命中的编号：被 422 拦下的人要能一眼看到是哪个触发的。
    assert "f4" in violations[0].detail


def test_bare_ref_leak_uses_the_run_fact_ids():
    text = "f18只说加速进入，没有解释责任划分。"
    assert evaluate_content_gate(text, fact_ids=["f18"]) != []
    assert evaluate_content_gate(text, fact_ids=["f1"]) == []


def test_clickbait_is_reported():
    violations = evaluate_content_gate("震惊！这件事居然是真的。")
    assert [v.code for v in violations] == [REASON_CLICKBAIT]
    assert "震惊" in violations[0].detail


def test_both_rules_can_fire_together():
    violations = evaluate_content_gate("震惊！（f4）里根本没写。")
    assert [v.code for v in violations] == [REASON_INTERNAL_REF, REASON_CLICKBAIT]


# ---------------------------------------------------------------------------
# 5. 接线：guardrail 只在违规时多出原因码
# ---------------------------------------------------------------------------


def test_guardrail_stays_silent_on_clean_content():
    """guardrail 的既有测试断言**精确列表**，所以干净内容必须一个码都不多。"""
    passed, reasons = evaluate(VALID, max_length=280)

    assert passed is True
    assert reasons == []


def test_guardrail_surfaces_gate_codes_on_violation():
    passed, reasons = evaluate("他没说清楚（f4）。", max_length=280)

    assert passed is False
    assert reasons == [REASON_INTERNAL_REF]


def test_guardrail_and_publish_share_one_rule_set():
    """两处调的必须是同一个函数——审核看到的理由与发布拦下的理由不能是两张清单。"""
    text = "震惊！（f4）里根本没写。"
    _, reasons = evaluate(text, max_length=280, fact_ids=["f18"])
    codes = [v.code for v in evaluate_content_gate(text, fact_ids=["f18"])]

    assert reasons == codes
