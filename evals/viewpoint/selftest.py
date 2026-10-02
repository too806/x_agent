"""Viewpoint Intelligence 离线基准的自检。

跑法：

    python -m evals.viewpoint.selftest

与 `evals/selftest.py` / `evals/style/selftest.py` 同一套纪律：断言的方向是
"**违规数据必须失败**"，而不是"干净数据应当通过"。

这里的反证有两条腿，缺一条就形同虚设：

1. **必须响的要响**：`cases.py` 里每一类的预期逐条核对；
2. **不该响的要静默**：`strong_evidence` 那一组必须零问题——
   只有"必须响"的检查，一个见啥都报的检测器能全过。

每条"检测器真的会响"的检查都额外做一次**反向扰动**：把触发它的那一项改回去，
码必须消失。这样才分得清"检测器在读那个输入"和"检测器碰巧每次都响"。

全程离线：不碰 X、不碰 LLM、不读数据库、不取挂钟时间。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields as dataclass_fields
from typing import Any

from evals._bootstrap import force_utf8_stdout

from evals.viewpoint import cases, scope
from evals.viewpoint.cases import CATEGORIES, make_materials, make_pack
from evals.viewpoint.scope import Observation

force_utf8_stdout()

from app.agent.schemas import (  # noqa: E402
    Angle,
    AngleDraft,
    ClaimScope,
    ViewpointStance,
)
from app.agent.viewpoint import (  # noqa: E402
    CONFIDENCE_WEIGHTS,
    SINGLE_SOURCE_EVIDENCE_CAP,
    STANCE_ORDER,
    VIEWPOINT_WEIGHTS,
    ViewpointScore,
)

_STANCE_INDEX = {stance: index for index, stance in enumerate(STANCE_ORDER)}


class ViewpointSelfTestFailure(AssertionError):
    """一条检查失败。与 `evals/selftest.py::SelfTestFailure` 同一角色。"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ViewpointSelfTestFailure(message)


# ---------------------------------------------------------------------------
# 预期的求值
# ---------------------------------------------------------------------------

#: `cases.py` 里允许出现的关系词。多一个词就意味着多一处需要在这里实现的分支，
#: 所以它是一份**封闭**的表——出现表外的词直接判失败，而不是猜一个语义。
RELATIONS = ("==", "!=", "<", "<=", ">", ">=", "is", "contains", "empty", "nonempty")

_SCORE_FIELDS = {field.name for field in dataclass_fields(ViewpointScore)}


def _resolve(score: ViewpointScore, field: str) -> Any:
    """从一条打分结果里取值。字段必须真的存在——拼错要报错，不能当默认值。"""
    if field not in _SCORE_FIELDS:
        raise ViewpointSelfTestFailure(
            f"预期里写了 `{field}`，但 ViewpointScore 没有这个字段"
        )
    return getattr(score, field)


def _satisfies(actual: Any, relation: str, expected: Any) -> bool:
    if relation == "is":
        return actual is expected
    if relation == "==":
        return bool(actual == expected)
    if relation == "!=":
        return bool(actual != expected)
    if relation == "<":
        return bool(actual < expected)
    if relation == "<=":
        return bool(actual <= expected)
    if relation == ">":
        return bool(actual > expected)
    if relation == ">=":
        return bool(actual >= expected)
    if relation == "contains":
        return expected in actual
    if relation == "empty":
        return len(actual) == 0
    if relation == "nonempty":
        return len(actual) > 0
    raise ViewpointSelfTestFailure(f"不认识的关系词 `{relation}`")


def _observe(case: cases.Case) -> Observation:
    return scope.observe_case(case)


def _by_claim(observation: Observation) -> dict[str, ViewpointScore]:
    return {score.claim: score for score in observation.scores}


def _ad_hoc(pack, materials, drafts) -> Observation:
    return scope.observe(pack, materials, drafts)


# ---------------------------------------------------------------------------
# 一、基准数据本身
# ---------------------------------------------------------------------------


def check_the_seven_categories_are_all_present() -> list[str]:
    """工作单第三十二节点名的七类必须真的在，且每类都有草稿与预期。

    "覆盖了七类"若只是文档里的一句话，它会一直是对的。这里让它是**可判的**：
    漏一类、多一类、加了一组没有预期的数据，都会红。

    同时挡住一种静默失效：一组数据带**空** `expect`，那么"逐条核对预期"
    那条检查会毫无意见地跳过它——看起来全绿，其实什么都没验。
    """
    present = tuple(case.category for case in cases.CASES)
    _check(
        present == CATEGORIES,
        f"基准里的类别与工作单点名的不一致：{present} != {CATEGORIES}",
    )
    _check(
        len(set(CATEGORIES)) == len(CATEGORIES), "类别名有重复，顺序比较就没有意义"
    )
    for case in cases.CASES:
        _check(case.drafts, f"{case.category} 没有草稿")
        _check(case.expect, f"{case.category} 没有预期，等于没验")
        _check(case.materials, f"{case.category} 没有材料，作者数会静默算成 0")
        _check(case.pack.facts, f"{case.category} 的证据包是空的")
    return [f"{len(cases.CASES)} 类，共 {sum(len(c.expect) for c in cases.CASES)} 条预期"]


def check_expectations_reference_real_fields() -> list[str]:
    """预期的字段名与关系词都必须合法。

    单列一条检查而不是让它炸在别处：`expect` 是**数据**，写错一个字段名
    会在核对时抛 `ViewpointSelfTestFailure`，而那条失败信息会指向检查函数，
    不是那行数据。这里一次把七组全扫一遍，错误信息就带上了类别名。
    """
    seen: set[str] = set()
    for case in cases.CASES:
        for item in case.expect:
            _check(
                len(item) == 4,
                f"{case.category} 的预期 {item!r} 不是 (下标, 字段, 关系, 期望值)",
            )
            position, field, relation, _ = item
            _check(
                0 <= position < len(case.drafts),
                f"{case.category} 的预期指向第 {position} 条草稿，但它没有那么多",
            )
            _check(
                field in _SCORE_FIELDS,
                f"{case.category} 的预期写了 `{field}`，ViewpointScore 没有这个字段",
            )
            _check(
                relation in RELATIONS,
                f"{case.category} 的预期用了不认识的关系词 `{relation}`",
            )
            seen.add(field)
    return [f"涉及 {len(seen)} 个字段：{'、'.join(sorted(seen))}"]


def check_every_case_matches_its_expectations() -> list[str]:
    """七组数据逐条核对它们的预期。这是这份基准的主体。"""
    notes: list[str] = []
    for case in cases.CASES:
        observation = _observe(case)
        for position, field, relation, expected in case.expect:
            actual = _resolve(observation.score(position), field)
            _check(
                _satisfies(actual, relation, expected),
                f"{case.category} 第 {position} 条：{field} {relation} {expected!r} 不成立"
                f"（实际 {actual!r}）",
            )
        notes.append(f"{case.category}：{len(case.expect)} 条预期全部成立")
    return notes


# ---------------------------------------------------------------------------
# 二、确定性
# ---------------------------------------------------------------------------


def check_scoring_is_bit_identical_across_runs() -> list[str]:
    """同一输入两次，逐字段相同——包括理由文本。

    理由也一起比：分项对得上而理由对不上，说明理由里有挂钟时间、集合迭代顺序
    之类的东西，而那正是"人拿着报告去复核"时会踩空的地方。
    """
    notes: list[str] = []
    for case in cases.CASES:
        first = _observe(case)
        second = _observe(case)
        _check(
            first.scores == second.scores,
            f"{case.category} 两次打分的结论不同",
        )
        notes.append(f"{case.category}：{len(first.scores)} 条结论逐字段一致")
    return notes


def check_draft_order_does_not_change_any_single_verdict() -> list[str]:
    """把草稿顺序倒过来，**每一条自己的结论**不变。

    不能直接比整个列表：`novelty` 要看同批的其他草稿，所以"第几条第几个"
    本来就会变。按 claim 对齐比才是这条性质真正的样子——顺序不该改变任何一条
    判断的内容，只该改变它们的排列。
    """
    notes: list[str] = []
    for case in cases.CASES:
        forward = _by_claim(_observe(case))
        backward = _by_claim(_ad_hoc(case.pack, case.materials, list(reversed(case.drafts))))
        _check(
            set(forward) == set(backward),
            f"{case.category} 倒序之后少/多了结论",
        )
        for claim, score in forward.items():
            _check(
                score == backward[claim],
                f"{case.category} 倒序后「{claim}」的结论变了",
            )
        notes.append(f"{case.category}：{len(forward)} 条结论与顺序无关")
    return notes


def check_confidence_does_not_contain_novelty() -> list[str]:
    """工作单第十五节：novelty 必须与 evidence_support 分开。

    直接断言**权重表本身**，而不只是"某条数据算出来没受影响"：后者在有人把
    novelty 以极小权重塞进去之后仍然可能通过。

    反证在下面：把 `restates_known_material` 打开（只动 novelty），confidence
    必须一动不动，而 viewpoint_score 必须掉。
    """
    names = [name for name, _ in CONFIDENCE_WEIGHTS]
    _check("novelty" not in names, f"confidence 的权重表里有 novelty：{names}")
    _check(
        abs(sum(weight for _, weight in CONFIDENCE_WEIGHTS) - 1.0) < 1e-9,
        "confidence 权重之和不等于 1",
    )
    _check(
        abs(sum(weight for _, weight in VIEWPOINT_WEIGHTS) - 1.0) < 1e-9,
        "viewpoint_score 权重之和不等于 1",
    )

    # 反证：只动 novelty 的那一项
    case = cases.STRONG
    plain = _ad_hoc(case.pack, case.materials, case.drafts).score(0)
    restating = AngleDraft(
        **{**case.drafts[0].model_dump(), "restates_known_material": True}
    )
    muted = _ad_hoc(case.pack, case.materials, [restating]).score(0)
    _check(
        muted.novelty < plain.novelty,
        f"打开 restates_known_material 没有压低 novelty（{muted.novelty} vs {plain.novelty}）",
    )
    _check(
        muted.confidence == plain.confidence,
        f"novelty 变了，confidence 也跟着变了（{muted.confidence} vs {plain.confidence}）"
        "——这两项必须是分开的",
    )
    _check(
        muted.viewpoint_score < plain.viewpoint_score,
        "novelty 掉了，viewpoint_score 却没掉——novelty 没有真的进入总分",
    )
    return [
        f"confidence 权重：{'、'.join(names)}",
        f"反证：novelty {plain.novelty} → {muted.novelty}，"
        f"confidence 恒为 {plain.confidence}，viewpoint_score "
        f"{plain.viewpoint_score} → {muted.viewpoint_score}",
    ]


def check_the_stored_confidence_is_never_an_input() -> list[str]:
    """工作单第十二节：分数必须能复算，"模型说 9.4 分"不行。

    `Angle.confidence` 是一个模型**可以**写进去的字段（5B.3 之前的运行里它就是
    模型给的）。反证：种一个 0.99 进去，算出来的 confidence 必须一模一样——
    打分函数一次都不许读它。
    """
    case = cases.WEAK
    # 这一条**故意**用 `Angle` 而不是 `AngleDraft`：被种进去的那个 0.99 是
    # 存储模型上的字段（5B.3 之前的运行里它就是模型给的），LLM 契约上没有它，
    # 所以这条反证只能在存储模型上做。用 `AngleDraft` 会直接 TypeError，
    # 而那看起来像"字段不存在"，与"字段存在但没人读"是两回事。
    seeded = Angle(**{**case.drafts[0].model_dump(), "confidence": 0.99})
    honest = _ad_hoc(case.pack, case.materials, case.drafts).score(0)
    polluted = _ad_hoc(case.pack, case.materials, [seeded]).score(0)
    _check(
        polluted.confidence == honest.confidence,
        f"存储里的 confidence 影响了计算结果（{polluted.confidence} vs {honest.confidence}）",
    )
    _check(
        polluted.confidence < 0.99,
        "种进去的 0.99 原样出现在结果里——那说明结果就是那个数",
    )
    return [f"种入 confidence=0.99，算出来仍是 {polluted.confidence}"]


# ---------------------------------------------------------------------------
# 三、上限与降级（反证都成对）
# ---------------------------------------------------------------------------


def check_the_single_source_cap_actually_bites() -> list[str]:
    """单一来源的支撑度上限必须真的生效，而不是"本来就低"。

    `evidence_support_raw` 存在的唯一理由就是这条检查能写出来。
    反证：同样三条事实，换成三条独立来源，支撑度必须越过那个上限。
    """
    case = cases.SINGLE_SOURCE
    capped = _observe(case).score(0)
    _check(
        capped.evidence_support_raw > SINGLE_SOURCE_EVIDENCE_CAP >= capped.evidence_support,
        f"上限没有咬住：raw={capped.evidence_support_raw} "
        f"最终={capped.evidence_support} 上限={SINGLE_SOURCE_EVIDENCE_CAP}",
    )

    spread_pack = make_pack(
        [
            ("f1", "这位作者说定价策略变了", ["s1"]),
            ("f2", "另一位说调整发生在上周", ["s2"]),
            ("f3", "第三位说企业版是重灾区", ["s3"]),
        ],
        counterpoints=["也有人说只是个别客户在谈价"],
        unknowns=["别的作者怎么看"],
    )
    spread = _ad_hoc(
        spread_pack,
        make_materials(s1="frank", s2="gina", s3="hugo"),
        case.drafts,
    ).score(0)
    _check(
        spread.evidence_support > SINGLE_SOURCE_EVIDENCE_CAP,
        f"换成三条来源之后支撑度仍是 {spread.evidence_support}，上限从未参与",
    )
    _check(
        spread.evidence_support == spread.evidence_support_raw,
        "多来源时 raw 与最终值应当相同",
    )
    return [
        f"单一来源：raw {capped.evidence_support_raw} → {capped.evidence_support}",
        f"反证：三条来源 {spread.evidence_support}",
    ]


def check_stance_never_exceeds_the_evidence_ceiling() -> list[str]:
    """立场上界由证据推出，模型只能更保守（工作单第九、十节）。

    两件事一起查：每一组的 `stance` 都不超过 `max_stance`；以及一条在弱证据上
    申报 `strong` 的草稿会被**降级**——而它申报的原值被保留，因为
    "模型有多经常超证据承诺"这个量要靠它才量得出来。
    """
    notes: list[str] = []
    for case in cases.CASES:
        for score in _observe(case).scores:
            _check(
                _STANCE_INDEX[score.stance] <= _STANCE_INDEX[score.max_stance],
                f"{case.category}：stance {score.stance.value} 超过了上界 "
                f"{score.max_stance.value}",
            )
        notes.append(f"{case.category}：立场均在上界之内")

    case = cases.WEAK
    overreaching = AngleDraft(
        **{**case.drafts[0].model_dump(), "stance": ViewpointStance.STRONG}
    )
    score = _ad_hoc(case.pack, case.materials, [overreaching]).score(0)
    _check(
        score.stance is ViewpointStance.UNCERTAIN,
        f"在弱证据上申报 strong，结果却是 {score.stance.value}",
    )
    _check(
        score.stance_declared is ViewpointStance.STRONG,
        "申报值没有保留——那就量不出模型超证据承诺的频率",
    )
    _check(
        score.max_stance is ViewpointStance.UNCERTAIN,
        f"弱证据的上界应当是 uncertain，实际 {score.max_stance.value}",
    )
    notes.append("反证：弱证据上申报 strong → 降级为 uncertain，申报值保留")
    return notes


def check_a_scope_gap_lowers_the_ceiling() -> list[str]:
    """overclaim 的代价必须是**可复算的**，不是一句提醒。

    `overclaim` 那一组给了天然的成对反证：同一批四条来源的证据、同一句反方，
    只把申报范围从 segment 改成 society，上界就从 strong 掉到 moderate。
    """
    case = cases.OVERCLAIM
    observation = _observe(case)
    narrow = observation.score(1)
    broad = observation.score(0)
    _check(
        narrow.max_stance is ViewpointStance.STRONG
        and broad.max_stance is ViewpointStance.MODERATE,
        f"申报范围没有改变上界：{narrow.max_stance.value} vs {broad.max_stance.value}",
    )
    # 反证的下一档：差 2 档以上要压到 cautious
    tiny_pack = make_pack(
        [("f1", "两家厂商上周同时调整了企业版报价", ["s1"])],
        counterpoints=["有客户说邮件里没有价格"],
        unknowns=["第三家会不会跟进"],
    )
    tiny = AngleDraft(
        claim="整个行业的价格体系都在重写",
        evidence_ids=["f1"],
        counterpoints=["也可能只是常规调整"],
        counterpoint_evidence_ids=["f1"],
        claim_scope=ClaimScope.SOCIETY,
    )
    score = _ad_hoc(tiny_pack, make_materials(s1="alice"), [tiny]).score(0)
    _check(
        score.scope_exceeds_evidence is True,
        "申报 society 而证据只有一条来源，却没有被判 overclaim",
    )
    _check(
        score.max_stance is ViewpointStance.CAUTIOUS,
        f"差 2 档以上的范围超标应当压到 cautious，实际 {score.max_stance.value}",
    )
    return [
        f"成对反证：segment → {narrow.max_stance.value}，society → {broad.max_stance.value}",
        f"差 2 档：上界 {score.max_stance.value}",
    ]


# ---------------------------------------------------------------------------
# 四、检测器真的在判（每条都带反向扰动）
# ---------------------------------------------------------------------------


def check_the_overclaim_detector_actually_fires() -> list[str]:
    """反证：把窄版**申报的范围**放宽到 society，码必须出现。

    这条反证刻意只动 `claim_scope`、不动正文：`scope_exceeds_evidence` 有两个
    来源（申报范围超出、正文出现全称量词），而这批证据撑得起 industry，
    正文里的全称量词在那里**不**触发（`overbroad_prose` 只在证据范围窄到
    case 以下时才看正文）。所以断言 `breadth_markers == ()` 是这条检查的关键：
    它证明码是"申报"那条路判出来的，与正文用词无关。
    """
    case = cases.OVERCLAIM
    observation = _observe(case)
    _check(
        scope.OVERCLAIM in observation.codes(0),
        "申报范围超过证据范围，overclaim 却没有报出来",
    )
    _check(
        scope.OVERCLAIM not in observation.codes(1),
        "窄版判断被误报 overclaim——那样这个码就没有分辨率",
    )

    widened = AngleDraft(
        **{**case.drafts[1].model_dump(), "claim_scope": ClaimScope.SOCIETY}
    )
    widened_score = _ad_hoc(case.pack, case.materials, [widened]).score(0)
    _check(
        widened_score.breadth_markers == (),
        f"这条反证要求正文里没有全称量词，实际命中 {widened_score.breadth_markers}",
    )
    _check(
        scope.OVERCLAIM in _ad_hoc(case.pack, case.materials, [widened]).codes(0),
        "只把申报范围放宽到 society，码却没有出现——检测器读的不是申报值",
    )
    return [
        "society 版被报，segment 版没有",
        "反证：只把申报值改成 society（正文一个全称量词都没有）→ 码出现",
    ]


def check_the_counterpoint_detector_reads_the_pack() -> list[str]:
    """反证：把证据包里的反方清空，码必须消失——否则它是在读别的东西。"""
    case = cases.CONFLICTING
    observation = _observe(case)
    _check(
        scope.COUNTERPOINT_IGNORED in observation.codes(1),
        "证据包里有反方、判断一条没回应，却没有报出来",
    )
    _check(
        scope.COUNTERPOINT_IGNORED not in observation.codes(0),
        "回应了反方的判断被误报——这个码会失去分辨率",
    )

    silent_pack = make_pack(
        [(f.fact_id, f.fact, list(f.source_ids)) for f in case.pack.facts],
        consensus=case.pack.consensus,
        unknowns=case.pack.unknowns,
    )
    _check(
        not silent_pack.counterpoints, "构造用的证据包没有真的清空反方"
    )
    _check(
        scope.COUNTERPOINT_IGNORED
        not in _ad_hoc(silent_pack, case.materials, case.drafts).codes(1),
        "证据包里已经没有反方，码却还在——它读的不是证据包",
    )
    return ["证据包有反方 → 第 2 条被报；反证：清空反方 → 码消失"]


def check_the_single_source_detector_actually_fires() -> list[str]:
    """反证：同一批事实摊到三条来源上，码必须消失。"""
    case = cases.SINGLE_SOURCE
    _check(
        scope.SINGLE_SOURCE in _observe(case).codes(0),
        "三条事实只有一条来源，却没有报单一来源",
    )
    spread_pack = make_pack(
        [
            ("f1", "这位作者说定价策略变了", ["s1"]),
            ("f2", "另一位说调整发生在上周", ["s2"]),
            ("f3", "第三位说企业版是重灾区", ["s3"]),
        ],
        counterpoints=["也有人说只是个别客户在谈价"],
        unknowns=["别的作者怎么看"],
    )
    _check(
        scope.SINGLE_SOURCE
        not in _ad_hoc(
            spread_pack, make_materials(s1="frank", s2="gina", s3="hugo"), case.drafts
        ).codes(0),
        "三条独立来源仍被报单一来源——码没有分辨率",
    )
    return ["单一来源被报；反证：三条来源后码消失"]


def check_no_evidence_and_low_support_are_two_different_codes() -> list[str]:
    """"什么都没引"与"引得太少"必须分得开，且两个码都可达。

    合起来会让报告读成同一句话，而这两件事的处置完全不同：前者是流程出错，
    后者是这次证据本来就不够。反证是各自补上证据之后码消失。
    """
    pack = make_pack(
        [
            ("f1", "一家厂商把企业版的起售价上调了", ["s1"]),
            ("f2", "另一家说暂时不跟", ["s2"]),
        ],
        unknowns=["是不是普遍现象"],
    )
    materials = make_materials(s1="alice", s2="bob")
    empty = AngleDraft(claim="这一次的调价恐怕会蔓延", evidence_ids=["f999"])
    _check(
        scope.NO_EVIDENCE in _ad_hoc(pack, materials, [empty]).codes(0),
        "一条事实都引不到，却没有报 no_evidence",
    )
    _check(
        scope.UNSUPPORTED not in _ad_hoc(pack, materials, [empty]).codes(0),
        "no_evidence 与 unsupported 同时出现，两个码就分不开了",
    )

    thin = AngleDraft(claim="这一次的调价恐怕会蔓延", evidence_ids=["f1"])
    thin_codes = _ad_hoc(pack, materials, [thin]).codes(0)
    _check(
        scope.UNSUPPORTED in thin_codes,
        f"两条事实只绑一条，却没有报 unsupported（实际 {thin_codes}）",
    )
    _check(
        scope.NO_EVIDENCE not in thin_codes, "绑到了事实却仍报 no_evidence"
    )

    thick = AngleDraft(claim="这一次的调价恐怕会蔓延", evidence_ids=["f1", "f2"])
    thick_codes = _ad_hoc(pack, materials, [thick]).codes(0)
    _check(
        scope.UNSUPPORTED not in thick_codes and scope.NO_EVIDENCE not in thick_codes,
        f"反证失败：把证据补全之后仍有 {thick_codes}",
    )
    return ["无证据 → no_evidence；只绑一条 → unsupported；补全 → 两者皆消失"]


def check_a_clean_viewpoint_raises_nothing() -> list[str]:
    """最重要的一条反证：干净数据必须**零问题**。

    没有它，一个见啥都报的检测器能通过上面每一条"必须响"的检查，
    而"报告里全是问题"和"检测器坏了"长得一模一样。
    """
    case = cases.STRONG
    observation = _observe(case)
    found = observation.codes()
    _check(not found, f"证据充分的那一组报了问题：{found}")
    note = observation.score(0)
    _check(
        note.stance is ViewpointStance.STRONG,
        f"四条来源、四位作者、已回应反方，立场却是 {note.stance.value}",
    )
    return [
        f"零问题；stance {note.stance.value}，confidence {note.confidence}，"
        f"viewpoint_score {note.viewpoint_score}"
    ]


def check_every_problem_code_is_reachable() -> list[str]:
    """每个声明的码都必须能被**某份输入**触发。

    照 `evals/selftest.py::check_every_gate_can_fail` 的形状：一个永远不会出现
    的码是装饰品，它会让人以为那件事被看着了。所以这里的清单是手工维护的，
    新增一个码而不给它数据，这条检查就红。
    """
    reachable: dict[str, str] = {}
    for case in cases.CASES:
        for code in _observe(case).codes():
            if isinstance(code, str):
                reachable.setdefault(code, case.category)

    # 基准七类没覆盖到的两个码，用构造数据补齐。
    # 三条事实 / 三条来源：只绑一条时支撑度 0.2792 < 0.35 → unsupported，
    # 而"绑不到任何事实"是另一条路（no_evidence），两者必须都能走到。
    pack = make_pack(
        [
            ("f1", "这家厂商说下周会公布新的价目表", ["s1"]),
            ("f2", "另一家说下个月跟进", ["s2"]),
            ("f3", "第三家的说法还没定", ["s3"]),
        ],
        unknowns=["会不会公布"],
    )
    materials = make_materials(s1="alice", s2="bob", s3="carol")
    for code, draft in (
        (
            scope.UNSUPPORTED,
            AngleDraft(claim="这次调整会波及后续的定价", evidence_ids=["f1"]),
        ),
        (
            scope.NO_EVIDENCE,
            AngleDraft(claim="这家厂商恐怕在试探市场", evidence_ids=["f404"]),
        ),
        (
            scope.UNVERIFIABLE_ORIGINALITY,
            AngleDraft(claim="这是全网第一个指出该趋势的判断", evidence_ids=["f1"]),
        ),
    ):
        if code not in reachable:
            codes = _ad_hoc(pack, materials, [draft]).codes(0)
            if code in codes:
                reachable[code] = "构造数据"

    missing = [code for code in scope.PROBLEM_CODES if code not in reachable]
    _check(
        not missing,
        f"这些码在任何输入上都不会出现，是装饰品：{missing}",
    )
    return [f"{code} ← {source}" for code, source in sorted(reachable.items())]


def check_a_prediction_is_only_punished_when_it_is_written_as_certainty() -> list[str]:
    """工作单第十七节：预测必须被标记为预测，不能当成事实写。

    `Angle` 里没有 `claim_type` 字段，所以这件事由**正文**判：写成"必然/
    注定"的会被 overclaim 抓住，写成"或许/可能"的不会。成对的数据在
    `cases.PREDICTION` 里，这里做一次反向扰动确认判的是那几个词。
    """
    case = cases.PREDICTION
    observation = _observe(case)
    certain = observation.score(0)
    hedged = observation.score(1)
    _check(
        scope.OVERCLAIM in observation.codes(0),
        f"把预测写成必然，却没有报 overclaim（markers={certain.breadth_markers}）",
    )
    _check(
        scope.OVERCLAIM not in observation.codes(1),
        f"写成预测的那条被误报（markers={hedged.breadth_markers}）",
    )
    _check(
        hedged.breadth_markers == (),
        f"hedged 版命中了全称量词：{hedged.breadth_markers}",
    )

    softened = AngleDraft(
        **{
            **case.drafts[0].model_dump(),
            "claim": "第三家或许会在下个月跟进",
        }
    )
    _check(
        scope.OVERCLAIM not in _ad_hoc(case.pack, case.materials, [softened]).codes(0),
        "把必然改成或许之后码还在——检测器读的不是正文",
    )
    return [
        f"必然版 → {scope.OVERCLAIM}（{certain.breadth_markers}）",
        "反证：改成「或许」→ 码消失",
    ]


# ---------------------------------------------------------------------------

CHECKS: tuple[tuple[str, Any], ...] = (
    ("工作单点名的七类都在，且都有预期", check_the_seven_categories_are_all_present),
    ("预期引用的字段与关系词都合法", check_expectations_reference_real_fields),
    ("七组数据全部满足各自的预期", check_every_case_matches_its_expectations),
    ("同一输入 → 同一结论（逐字段）", check_scoring_is_bit_identical_across_runs),
    ("草稿顺序不改变任何一条的判断", check_draft_order_does_not_change_any_single_verdict),
    ("confidence 里没有 novelty", check_confidence_does_not_contain_novelty),
    ("存储里的 confidence 从不进入计算", check_the_stored_confidence_is_never_an_input),
    ("单一来源上限确实咬住（反证）", check_the_single_source_cap_actually_bites),
    ("立场上界由证据推出，模型只能更保守（反证）", check_stance_never_exceeds_the_evidence_ceiling),
    ("申报范围超标会压低上界（成对反证）", check_a_scope_gap_lowers_the_ceiling),
    ("overclaim 检测器确实会响（反证）", check_the_overclaim_detector_actually_fires),
    ("反方检测器读的是证据包（反证）", check_the_counterpoint_detector_reads_the_pack),
    ("单一来源检测器确实会响（反证）", check_the_single_source_detector_actually_fires),
    ("无证据与低支撑是两个码（反证）", check_no_evidence_and_low_support_are_two_different_codes),
    ("干净观点零问题（反证）", check_a_clean_viewpoint_raises_nothing),
    ("每个问题码都可达", check_every_problem_code_is_reachable),
    ("预测只在写成必然时才被判", check_a_prediction_is_only_punished_when_it_is_written_as_certainty),
)


def run_selftest(*, verbose: bool = False, as_json: bool = False) -> int:
    """跑全部检查。返回进程退出码：0 = 全过。"""
    results: list[dict[str, Any]] = []
    failures = 0
    for name, check in CHECKS:
        try:
            notes = check()
        except Exception as exc:  # noqa: BLE001 - 一条检查的失败不该中断其余检查
            failures += 1
            results.append(
                {"check": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
            )
            if not as_json:
                print(f"✗ {name}")
                print(f"    {type(exc).__name__}: {exc}")
            continue
        results.append({"check": name, "passed": True, "notes": notes})
        if not as_json:
            print(f"✓ {name}")
            if verbose:
                for note in notes:
                    print(f"    {note}")

    if as_json:
        print(
            json.dumps(
                {
                    "checks": results,
                    "passed": len(CHECKS) - failures,
                    "failed": failures,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"\nViewpoint 自检：{len(CHECKS) - failures} 通过 / {failures} 失败"
            f"（共 {len(CHECKS)} 项）"
        )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/viewpoint/selftest.py",
        description="Viewpoint Intelligence 离线基准的自检（不碰 X、不碰 LLM）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条检查的说明")
    args = parser.parse_args(argv)
    return run_selftest(verbose=args.verbose, as_json=args.json)


if __name__ == "__main__":  # pragma: no cover - 入口
    sys.exit(main())


__all__ = [
    "CHECKS",
    "ViewpointSelfTestFailure",
    "run_selftest",
    "main",
]
