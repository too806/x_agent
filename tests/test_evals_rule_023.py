"""RULE-023 对**动机/意图**两个字段的覆盖（第二轮：补上漏比的那两项）。

## 背景：一次真实的"漏比"

`evals/selftest.py::check_every_scored_field_is_reproduced` 这条自检的失败信息是：

    RULE-023 漏比的字段：['motive_markers', 'motive_unsupported']

也就是说：`viewpoint.score_viewpoints` **算得出**这两个字段、`angles.py::_assessment_of`
也**写得进** `ViewpointAssessment`，但 RULE-023 的比对清单里没有它们——
于是"这两个数有没有被复算过"这件事，在报告里与"比过而且相等"长得一模一样。
这正是那条自检存在的理由：**少比一项，就是给那一项留了一条可以悄悄变的路**。

## 为什么这两个字段用**同一套**断言

它们与 `breadth_markers` 是同一类东西——**打分时算出来的确定性痕迹**，不是
LLM 的自由输出：

- `motive_markers` = `find_motive_markers(claim)` 的逐字命中（`viewpoint.py`），
  一个 `list[str]`；
- `motive_unsupported` = "命中了动机词 **且** 独立来源不足 2 条"这个条件性下界，
  一个 `bool`。

两者都由同一组输入（claim + 绑定到的来源/作者）唯一定出，所以验证方式就是
RULE-023 本来的方式：同一组输入再算一遍，逐项比对。**没有为它们另造一套断言**
——`_differs` 已经分得清"`list` 与 `tuple` 的同一个序列"（不算差异）和
"`True` 与 `1`"（算差异，布尔按 `is` 比），这两件事下面各有反例钉住。
"""

from evals.analysis import analyze
from evals.behavior import evaluate_rules
from evals.behavior.rules import (
    _VIEWPOINT_ANGLE_FIELDS,
    _VIEWPOINT_ASSESSMENT_FIELDS,
    _differs,
)
from evals.loader import load

CLEAN = "evals/fixtures/viewpoint_supported.json"


def _status(record, rule_id: str = "RULE-023") -> str:
    report = evaluate_rules(analyze(record))
    for result in report.results:
        if result.rule_id == rule_id:
            return result.status
    raise AssertionError(f"{rule_id} 没有出现在结果里")


def _with_assessment(record, **updates):
    """把第一个**带评估明细**的观点改几个字段，其余原样不动。

    只改 `assessment` 上的字段：这正是"记录下来的评估明细被人改了一个值"
    的真实形态，也是 RULE-023 唯一该抓到的那种不一致。
    """
    angles = list(record.state.angles)
    for index, angle in enumerate(angles):
        if angle.assessment is None:
            continue
        angles[index] = angle.model_copy(
            update={"assessment": angle.assessment.model_copy(update=updates)}
        )
        return record.model_copy(
            update={"state": record.state.model_copy(update={"angles": angles})}
        )
    raise AssertionError("这份 fixture 里没有带评估明细的观点，测不了")


# ---------------------------------------------------------------------------
# 1. 清单本身：两个字段必须在，且落在它该在的那一层
# ---------------------------------------------------------------------------


def test_the_two_motive_fields_are_both_in_the_comparison_list():
    """清单里必须有这两个字段，且**只能出现在明细那一组**里。

    出现在 `_VIEWPOINT_ANGLE_FIELDS` 里会变成另一种错：`getattr(angle, "motive_markers")`
    当场抛 `AttributeError`（`Angle` 上没有这个字段），RULE-023 于是从"漏比"
    变成"执行失败"——一样是没在查它。
    """
    assert "motive_markers" in _VIEWPOINT_ASSESSMENT_FIELDS
    assert "motive_unsupported" in _VIEWPOINT_ASSESSMENT_FIELDS
    assert "motive_markers" not in _VIEWPOINT_ANGLE_FIELDS
    assert "motive_unsupported" not in _VIEWPOINT_ANGLE_FIELDS


def test_the_two_fields_really_exist_on_the_stored_shape():
    """两个字段真的落在 `ViewpointAssessment` 上——清单写了不存在的字段，
    比对会在 `getattr` 那里炸，而不是安静地什么都不查。"""
    from app.agent.schemas import ViewpointAssessment

    assert "motive_markers" in ViewpointAssessment.model_fields
    assert "motive_unsupported" in ViewpointAssessment.model_fields


# ---------------------------------------------------------------------------
# 2. 真的查得动：两个字段各自被改一个值时，RULE-023 必须失败
# ---------------------------------------------------------------------------


def test_a_clean_fixture_passes_first():
    """先说清楚对照组是干净的。

    没有这一条，下面两条"改了就该红"的断言可以被一个**永远失败**的实现满足，
    而那种实现会把每份数据都标红，然后被人关掉。
    """
    assert _status(load(CLEAN)) == "pass"


def test_a_drifted_motive_marker_is_caught():
    """记录下来的 `motive_markers` 与复算结果不一致 → 失败，且说得出是哪个字段。

    构造的是最容易发生的那种漂移：claim 里其实没有动机词（记录为空），
    而明细里被填成了 `["意图"]`。反过来（真命中却被清空）同样该被抓到——
    后者更危险，它会让一条**该被拦下的**动机断言看起来没问题。
    """
    record = _with_assessment(load(CLEAN), motive_markers=["意图"])

    report = evaluate_rules(analyze(record))
    result = next(r for r in report.results if r.rule_id == "RULE-023")

    assert result.status == "fail"
    assert "assessment.motive_markers" in result.detail


def test_a_drifted_motive_verdict_is_caught():
    """`motive_unsupported` 被改动 → 失败。

    它是一个布尔，而布尔在这里是**判断**本身（"这条动机断言有没有被证据托住"）。
    它值错了，`accept_ceiling_for` 那一侧的上界就跟着错——一条该被改判的
    accept 会溜过去。
    """
    clean = load(CLEAN)
    stored = next(
        angle.assessment.motive_unsupported
        for angle in clean.state.angles
        if angle.assessment is not None
    )
    record = _with_assessment(clean, motive_unsupported=not stored)

    report = evaluate_rules(analyze(record))
    result = next(r for r in report.results if r.rule_id == "RULE-023")

    assert result.status == "fail"
    assert "assessment.motive_unsupported" in result.detail


# ---------------------------------------------------------------------------
# 3. 比对口径：与其余字段共用一套规则，两个反例
# ---------------------------------------------------------------------------


def test_a_list_and_a_tuple_of_the_same_items_are_not_a_difference():
    """存下来的是 `list`（过了 pydantic），复算的是 `tuple`（打分器的原样返回）。

    直接 `!=` 会把"一个字不差"判成不可复现，于是这条规则永远响——
    而永远响的检测器和永远不响的一样没用。`motive_markers` 正是这样一个
    列表字段，所以这条口径必须钉住。
    """
    assert _differs(["意图"], ("意图",)) is False
    assert _differs(["意图", "动机"], ("意图", "动机")) is False
    assert _differs([], ()) is False


def test_a_sequence_difference_is_a_difference():
    """逐项比，不是"都非空"就算相等：少一个、多一个、换一个都要算差异。"""
    assert _differs(["意图"], ("意图", "动机")) is True
    assert _differs(["意图", "动机"], ("意图",)) is True
    assert _differs(["动机"], ("意图",)) is True


def test_a_bool_is_not_a_number_here():
    """`motive_unsupported` 是布尔：`True` 与 `1` 必须算**不一样**。

    `True == 1` 在 Python 里成立，用数值容差那条分支去比会把它判成相同，
    于是"这个布尔被改成了 1"这条漂移查不出来。
    """
    assert _differs(True, 1) is True
    assert _differs(False, 0) is True
    assert _differs(True, True) is False


def test_float_tolerance_still_applies_to_the_scores():
    """补一句反证：容差那条分支没有被这次改动破坏（它管的是其余二十多项）。"""
    assert _differs(8.4, 8.4000001) is False
    assert _differs(8.4, 8.5) is True
