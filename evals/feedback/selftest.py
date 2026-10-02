"""Feedback 离线基准的自检（工作单第三十节）。

跑法：

    python -m evals.feedback.selftest

与 `evals/selftest.py` / `evals/viewpoint/selftest.py` 同一套纪律：断言的方向是
"**违规数据必须失败**"，而不是"干净数据应当通过"。这里的反证有两条腿，缺一条
就形同虚设：

1. **必须响的要响**：`cases.py` 里每一类的预期逐条核对；
2. **不该响的要静默**：`healthy_sample` 那一组必须零问题——只有"必须响"的
   检查，一个见啥都报的检测器能全过。

第三十条点名的六项检查（`snapshot_time_semantics` / `null_metrics_not_zero` /
`derived_metric_requirements` / `small_sample_guard` / `causal_language_guard` /
`feedback_traceability`）在这里逐项实现，每项都配一次**反向扰动**：把触发它的
那一项改回正常值，结论必须消失。这样才分得清"检测器在读那个输入"和"检测器
碰巧每次都响"。

全程离线：不碰 X、不碰 LLM、不读数据库、不取挂钟时间。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from evals._bootstrap import force_utf8_stdout

from evals.feedback import cases
from evals.feedback.cases import CATEGORIES, Case

force_utf8_stdout()

from app.feedback.metrics import (  # noqa: E402
    Metrics,
    WINDOW_BOUNDARY_HOURS,
    WINDOW_LABELS,
    attention_share,
    conversation_share,
    engagement_rate,
    engagement_total,
    rate,
    window_bucket,
)
from app.feedback.report import (  # noqa: E402
    MAX_CONTEXT_SIGNALS,
    build_feedback_context,
    build_report,
)
from app.feedback.schema import (  # noqa: E402
    FeedbackReport,
    FeedbackSignal,
    FeedbackSignalKind,
)
from app.feedback.signals import (  # noqa: E402
    CAUSAL_MARKERS,
    MIN_SAMPLES_FOR_COMPARISON,
    assert_no_causal_claim,
    build_signals,
    find_causal_language,
    find_instruction_language,
)


class FeedbackSelfTestFailure(AssertionError):
    """一条检查失败。与 `evals/selftest.py::SelfTestFailure` 同一角色。"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise FeedbackSelfTestFailure(message)


# ---------------------------------------------------------------------------
# 预期的求值
# ---------------------------------------------------------------------------

#: 允许出现的关系词。多一个词就意味着多一处需要在这里实现的分支，所以它是
#: 一份**封闭**的表——出现表外的词直接判失败，而不是猜一个语义。
RELATIONS = (
    "==",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
    "is",
    "contains",
    "not_contains",
    "empty",
    "nonempty",
)

#: 判"这是不是一个比较类信号"用的集合。**在这里独立写一份**而不是从
#: `rules.py` import：两个地方各写一份，改了生产却没改这里才会红。
_COMPARATIVE_KINDS = frozenset(
    {
        FeedbackSignalKind.ATTENTION,
        FeedbackSignalKind.CONVERSATION,
        FeedbackSignalKind.AMPLIFICATION,
        FeedbackSignalKind.SHARE,
    }
)

_REPORT_FIELDS = frozenset(
    {
        "sample_count",
        "signal_count",
        "signal_kinds",
        "limitation_count",
        "insufficient_present",
        "comparative_present",
    }
)

_SIGNAL_FIELDS = frozenset(set(FeedbackSignal.model_fields) | {"evidence_count"})


def _resolve(report: FeedbackReport, index: int | None, field: str) -> Any:
    """从报告里取值。下标为 `None` 表示报告层；字段必须真的存在。"""
    if index is None:
        if field not in _REPORT_FIELDS:
            raise FeedbackSelfTestFailure(
                f"预期里写了报告层字段 `{field}`，但它不存在；"
                f"可用的是 {sorted(_REPORT_FIELDS)}"
            )
        if field == "sample_count":
            return report.sample_count
        if field == "signal_count":
            return len(report.signals)
        if field == "signal_kinds":
            return tuple(signal.kind.value for signal in report.signals)
        if field == "limitation_count":
            return len(report.limitations)
        if field == "insufficient_present":
            return any(
                signal.kind is FeedbackSignalKind.INSUFFICIENT_SAMPLE
                for signal in report.signals
            )
        return any(signal.kind in _COMPARATIVE_KINDS for signal in report.signals)

    if not 0 <= index < len(report.signals):
        raise FeedbackSelfTestFailure(
            f"预期指向第 {index} 条信号，但报告只有 {len(report.signals)} 条"
        )
    signal = report.signals[index]
    if field == "evidence_count":
        return len(signal.evidence)
    if field == "kind":
        return signal.kind.value
    if field not in _SIGNAL_FIELDS:
        raise FeedbackSelfTestFailure(f"信号没有字段 `{field}`")
    return getattr(signal, field)


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
    if relation == "not_contains":
        return expected not in actual
    if relation == "empty":
        return len(actual) == 0
    if relation == "nonempty":
        return len(actual) > 0
    raise FeedbackSelfTestFailure(f"不认识的关系词 `{relation}`")


def _report_for(case: Case) -> FeedbackReport:
    return build_report(case.observations)


def _kinds(report: FeedbackReport) -> tuple[str, ...]:
    return tuple(signal.kind.value for signal in report.signals)


# ---------------------------------------------------------------------------
# 一、基准数据本身
# ---------------------------------------------------------------------------


def check_the_six_categories_are_all_present() -> list[str]:
    """工作单第三十二节点名的六类必须真的在，且每类都有观察与预期。

    "覆盖了六类"若只是文档里的一句话，它会一直是对的。这里让它是**可判的**：
    漏一类、多一类、加了一组没有预期的数据，都会红。

    同时挡住一种静默失效：一组数据带**空** `expect`，那么"逐条核对预期"
    那条检查会毫无意见地跳过它——看起来全绿，其实什么都没验。
    """
    present = tuple(case.category for case in cases.CASES)
    _check(
        present == CATEGORIES,
        f"基准里的类别与工作单点名的不一致：{present} != {CATEGORIES}",
    )
    _check(len(set(CATEGORIES)) == len(CATEGORIES), "类别名有重复，顺序比较就没有意义")
    for case in cases.CASES:
        _check(case.observations, f"{case.category} 没有观察")
        _check(case.expect, f"{case.category} 没有预期，等于没验")
        _check(
            all(observation.snapshot_id for observation in case.observations),
            f"{case.category} 的观察没有 id，可追溯性无从谈起",
        )
    return [f"{len(cases.CASES)} 类，共 {sum(len(c.expect) for c in cases.CASES)} 条预期"]


def check_expectations_reference_real_fields() -> list[str]:
    """预期的字段名与关系词都必须合法。

    单列一条检查而不是让它炸在别处：`expect` 是**数据**，写错一个字段名会在
    核对时抛 `FeedbackSelfTestFailure`，而那条失败信息会指向检查函数，
    不是那行数据。这里一次把六组全扫一遍，错误信息就带上了类别名。
    """
    for case in cases.CASES:
        for item in case.expect:
            _check(
                len(item) == 4,
                f"{case.category} 的预期 {item!r} 不是 (下标, 字段, 关系, 期望值)",
            )
            index, field, relation, _ = item
            _check(
                index is None or index >= 0,
                f"{case.category} 的预期下标不能为负数：{index!r}",
            )
            _check(
                relation in RELATIONS,
                f"{case.category} 的预期用了不认识的关系词 `{relation}`",
            )
            _check(
                field in _REPORT_FIELDS or field in _SIGNAL_FIELDS,
                f"{case.category} 的预期写了不存在的字段 `{field}`",
            )
    return [f"六组预期共 {sum(len(c.expect) for c in cases.CASES)} 条，字段与关系词都合法"]


def check_every_case_matches_its_expectations() -> list[str]:
    """六组数据逐条核对它们的预期。这是这份基准的主体。"""
    notes: list[str] = []
    for case in cases.CASES:
        report = _report_for(case)
        for index, field, relation, expected in case.expect:
            actual = _resolve(report, index, field)
            where = "报告" if index is None else f"第 {index} 条信号"
            _check(
                _satisfies(actual, relation, expected),
                f"{case.category} {where}：{field} {relation} {expected!r} 不成立"
                f"（实际 {actual!r}）",
            )
        notes.append(f"{case.category}：{len(case.expect)} 条预期全部成立")
    return notes


# ---------------------------------------------------------------------------
# 二、第三十节点名的六项
# ---------------------------------------------------------------------------


def check_snapshot_time_semantics() -> list[str]:
    """`snapshot_time_semantics`：分档只由**存下来的** `hours_since_publish` 决定。

    三件事一起查：

    - 观察时刻与发布时刻的差**不在这里重算**——`hours_since_publish` 是写入时
      算好的，读的时候只分档（工作单第九节要求两个时间语义始终分开）；
    - `None` 进 `None` 出；
    - **负数返回 `None`**，而不是被塞进 `0-1h`：观察时刻早于发布时刻说明数据
      本身不自洽，硬塞进第一档会把一个数据问题伪装成一条正常观察。

    反证：同一条观察只把小时数挪一档，档位必须跟着挪。
    """
    for boundary, label in zip(WINDOW_BOUNDARY_HOURS, WINDOW_LABELS):
        _check(
            window_bucket(boundary - 1e-9) == label,
            f"{boundary - 1e-9} 小时应当落在 {label}",
        )
    for boundary, next_label in zip(WINDOW_BOUNDARY_HOURS, WINDOW_LABELS[1:]):
        _check(
            window_bucket(boundary) == next_label,
            f"{boundary} 小时应当落在 {next_label}（边界归下一档）",
        )
    _check(window_bucket(None) is None, "未知的小时数必须返回 None，不能猜一档")
    _check(window_bucket(-0.1) is None, "负数小时数必须返回 None，不能塞进 0-1h")

    # 负数**不得**悄悄变成第一档：单列一条，因为"塞进最近的一档"是一种
    # 很好心、也很容易写出来的兜底。
    _check(
        window_bucket(-1.0) != WINDOW_LABELS[0],
        "负数小时被塞进了第一档——那会把一个数据问题伪装成一条正常观察",
    )

    case = cases.MULTIPLE_WINDOWS
    report = _report_for(case)
    _check(
        any("本次观察覆盖的时间档" in note for note in report.limitations),
        "报告没有说明这次观察覆盖了哪些时间档",
    )
    # 反证：同一批观察，只把小时数挪一档，覆盖说明必须跟着变。
    shifted = tuple(
        cases.obs(
            index,
            hours=observation.hours_since_publish + 1.0
            if observation.hours_since_publish is not None
            else None,
            likes=observation.likes,
            replies=observation.replies,
            reposts=observation.reposts,
            quotes=observation.quotes,
        )
        for index, observation in enumerate(case.observations, start=1)
    )
    shifted_report = build_report(shifted)
    _check(
        shifted_report.limitations != report.limitations,
        "把所有观察往后挪一小时，覆盖说明一字未变——它读的不是 hours_since_publish",
    )
    return [
        "分档边界与标签一一对应；None 与负数都返回 None",
        "反证：整体后移一小时 → 覆盖说明随之改变",
    ]


def check_null_metrics_not_zero() -> list[str]:
    """`null_metrics_not_zero`：读不到就是读不到，绝不按 0 计入。

    三件事一起查：

    - `engagement_total` 只要四项里有任意一项未知就是 `None`
      （把已知的三项加起来，会把"未知"变成一个更小的、看起来真实的数字）；
    - 比率在分母缺失或 `<= 0` 时是 `None`，**不是 0.0**；
    - `engagement_rate`（互动/曝光）在 v1 恒为 `None`——曝光从未采集。

    反证：把四项补齐，总量与占比必须立刻算得出来。
    """
    partial = Metrics(likes=10, replies=None, reposts=2, quotes=1)
    _check(
        engagement_total(partial) is None,
        "四项里有一项未知，总量仍算了出来——那是在替未知编一个数",
    )
    _check(
        attention_share(partial) is None,
        "分母未知时占比仍算了出来（原始实现里它会变成分子除以一个更小的数）",
    )
    complete = Metrics(likes=10, replies=5, reposts=2, quotes=3)
    _check(engagement_total(complete) == 20, "四项齐全时总量必须算得出来")
    _check(
        abs(attention_share(complete) - 0.5) < 1e-9,
        f"四项齐全时注意力占比应当是 0.5，实际 {attention_share(complete)}",
    )

    _check(rate(1, None) is None, "分母未知时比率必须是 None")
    _check(rate(1, 0) is None, "分母为 0 时比率必须是 None，不是 0.0")
    _check(rate(None, 3) is None, "分子未知时比率必须是 None")
    _check(rate(3, 4) == 0.75, "分子分母都已知时比率必须算得出来")

    # v1 的曝光从未采集：互动率没有分母，因此恒为 None。反证在下一行——
    # 补上一个曝光量，它立刻有值。这两行合起来才说明"恒为 None"不是写死的。
    _check(
        engagement_rate(complete) is None,
        "曝光量未知时互动率必须为 None——不是零互动率",
    )
    _check(
        engagement_rate(
            Metrics(likes=10, replies=5, reposts=2, quotes=3, impressions=1000)
        )
        == 0.02,
        "有了曝光量之后互动率仍算不出来——它读的不是 impressions",
    )

    case = cases.MISSING_METRIC
    report = _report_for(case)
    attention = report.signals[0]
    _check(
        attention.sample_count == 5,
        f"缺 3 条点赞读数的注意力信号应当只依据 5 条观察，实际 {attention.sample_count}",
    )
    # 反证：把缺的读数补上，依据的观察数必须涨到 8。
    filled = tuple(
        cases.obs(
            index,
            hours=observation.hours_since_publish,
            likes=observation.likes if observation.likes is not None else 1,
            replies=observation.replies,
            reposts=observation.reposts,
            quotes=observation.quotes,
        )
        for index, observation in enumerate(case.observations, start=1)
    )
    filled_report = build_report(filled)
    _check(
        filled_report.signals[0].sample_count == 8,
        "补齐读数之后依据的观察数没有变——注意力信号读的不是 likes",
    )
    return [
        "部分已知 → None；四项齐全 → 20；占比 0.5",
        "反证：补齐缺失读数 → 注意力信号依据的观察数 5 → 8",
    ]


def check_derived_metric_requirements() -> list[str]:
    """`derived_metric_requirements`：每个派生量都对得上它自己要的东西。

    这一条问的是"这个数**凭什么是这么算的**"：分母是谁、缺了会怎样、
    有没有把不该合的合了。三类互动必须是三个独立的量（工作单第十二节），
    不能被合成一个"热度"。
    """
    one = Metrics(likes=4, replies=6, reposts=3, quotes=2)
    _check(engagement_total(one) == 15, "总量必须是四项之和")
    # 对话与扩散的**分母**都是总量，不是"点赞数"：这两类行为不是点赞的子集。
    _check(
        abs(conversation_share(one) - 8 / 15) < 1e-9,
        f"对话占比应当是 8/15，实际 {conversation_share(one)}",
    )
    _check(
        abs(attention_share(one) - 4 / 15) < 1e-9,
        f"注意力占比应当是 4/15，实际 {attention_share(one)}",
    )

    # 分母为 0（四类计数全为零）时，占比没有被定义——返回 0.0 会读成
    # "测出来是零互动"，那与"没有任何互动"是同一件事吗？不是：这里连
    # 分母都不成立，所以正确处置是"算不出来"。
    zero = Metrics(likes=0, replies=0, reposts=0, quotes=0)
    _check(engagement_total(zero) == 0, "四项都是 0 时总量是 0（这不是未知）")
    _check(
        attention_share(zero) is None,
        "总量为 0 时占比必须算不出来，不能返回 0.0",
    )
    return [
        "总量 = 四类之和；三类占比的分母都是总量",
        "总量为 0 → 占比 None（不是 0.0）",
    ]


def check_small_sample_guard() -> list[str]:
    """`small_sample_guard`：低于下限就什么都不比，而且不把下限伪装成显著性。

    两件事一起查：

    - 低于下限时**恰好**产出一条信号，它只带 n，不带任何比较、比率或排名；
    - 那条信号的措辞必须把"采集量的门槛"与"统计显著性判断"分开说——
      工作单第十七节要求这个区别写在明面上，免得一个拍出来的数字被读成统计结论。

    反证：把观察补到下限，比较类信号必须立刻出现。
    """
    floor = MIN_SAMPLES_FOR_COMPARISON
    for n in range(1, floor):
        observations = [
            cases.obs(i, hours=float(i), likes=10 + i, replies=1, reposts=0, quotes=0)
            for i in range(1, n + 1)
        ]
        signals = build_signals(observations)
        _check(
            len(signals) == 1,
            f"n={n} 低于下限 {floor} 时应当只有一条信号，实际 {len(signals)} 条",
        )
        _check(
            signals[0].kind is FeedbackSignalKind.INSUFFICIENT_SAMPLE,
            f"n={n} 的第一条信号是 {signals[0].kind.value}，不是 insufficient_sample",
        )
        _check(signals[0].sample_count == n, "样本不足的信号必须如实带上 n")
        _check(signals[0].metric is None, "样本不足的信号不该指向某个度量")
        _check(
            "不是统计显著性判断" in signals[0].statement,
            "样本不足的措辞没有把「下限」与「显著性」分开——它会被读成一个统计结论",
        )
        _check(
            not any(signal.kind in _COMPARATIVE_KINDS for signal in signals),
            f"n={n} 低于下限却产出了比较类信号",
        )

    # 反证：补到下限，比较类信号出现，样本不足的信号消失。
    at_floor = [
        cases.obs(i, hours=float(i), likes=10 + i, replies=1, reposts=0, quotes=0)
        for i in range(1, floor + 1)
    ]
    signals = build_signals(at_floor)
    _check(
        any(signal.kind in _COMPARATIVE_KINDS for signal in signals),
        f"n={floor} 已经到下限，却仍然一条比较类信号都没有",
    )
    _check(
        not any(signal.kind is FeedbackSignalKind.INSUFFICIENT_SAMPLE for signal in signals),
        f"n={floor} 已经到下限，样本不足的信号却还在",
    )

    report = _report_for(cases.SMALL_SAMPLE)
    _check(
        sum(1 for s in report.signals if s.kind is FeedbackSignalKind.INSUFFICIENT_SAMPLE) == 1,
        "低于下限时样本不足的信号必须**恰好**一条",
    )
    _check(
        any("低于比较下限" in note for note in report.limitations),
        "报告没有把样本量不足写进局限说明",
    )
    return [
        f"n=1..{floor - 1} 全部只产出 1 条只带 n 的信号；n={floor} 出现比较类信号",
        "措辞里明确写出「不是统计显著性判断」",
    ]


def check_causal_language_guard() -> list[str]:
    """`causal_language_guard`：代码生成的每一句话都不许声称因果。

    分三层查，缺一层这条检查就只是装饰：

    1. **标记表本身有效**：`assert_no_causal_claim` 真的会对一句因果句抛错；
    2. **它读的是那句话**：把标记词去掉，异常必须消失（否则它是个恒抛的桩）；
    3. **生产的产出干净**：六组数据产出的每一条 `statement` 都过一遍检查。

    另外查指令措辞：反馈不是"下次你要怎么写"（工作单第四十二节）。
    """
    causal_sentence = "这条帖子带来了更多点赞。"
    try:
        assert_no_causal_claim(causal_sentence)
    except ValueError:
        pass
    else:
        raise FeedbackSelfTestFailure("因果句没有被拦下——标记表形同虚设")

    _check(
        find_causal_language("这条帖子带来了更多点赞。"),
        "因果检测器没有认出「带来」",
    )
    _check(
        not find_causal_language("这条帖子的点赞数中位数是 22.5。"),
        "因果检测器在一句纯观察上误报——它会对每一句话都响",
    )
    _check(
        "因为" in CAUSAL_MARKERS and "because" in CAUSAL_MARKERS,
        "标记表必须中英双语，否则英文模板会从旁边走过去",
    )

    for case in cases.CASES:
        report = _report_for(case)
        for index, signal in enumerate(report.signals):
            _check(
                not find_causal_language(signal.statement),
                f"{case.category} 第 {index} 条信号含因果措辞：{signal.statement!r}",
            )
            _check(
                not find_instruction_language(signal.statement),
                f"{case.category} 第 {index} 条信号含指令措辞：{signal.statement!r}",
            )
    # 局限说明也去看：它同样会被渲染进 Writer 的上下文。
    for case in cases.CASES:
        report = _report_for(case)
        for note in report.limitations:
            _check(
                not find_instruction_language(note),
                f"{case.category} 的局限说明含指令措辞：{note!r}",
            )
    # 再补一发中文的连词式因果句：上面那句命中的是"带来"（动词），
    # 这一句命中的是"所以/导致"（连词），两条不同的分支都得真的会响。
    try:
        assert_no_causal_claim("点赞与转发同时出现，所以它导致了增长。")
    except ValueError:
        pass
    else:
        raise FeedbackSelfTestFailure("连词式因果断言没有被拦下")
    return [
        f"{sum(len(_report_for(c).signals) for c in cases.CASES)} 条信号的措辞零因果、零指令",
        "反证：因果句被拦下，去掉标记词后不再命中",
    ]


def check_feedback_traceability() -> list[str]:
    """`feedback_traceability`：没有依据就没有结论，依据必须真的存在。

    三件事一起查：

    - 每条信号的 `evidence` 非空，且每个 id 都真的出现在这份报告的观察里；
    - `FeedbackContext.source_snapshot_ids` 与报告的观察一一对应，
      这样事后能从 checkpoint 回到原始快照（工作单第二十五节）；
    - 引用了不存在的 id 时，报告的出口必须抛错，而不是带着一条查不到
      依据的结论出门。

    反证：把一条真实 id 换掉，出口必须立刻不通过。
    """
    for case in cases.CASES:
        report = _report_for(case)
        known = {observation.snapshot_id for observation in report.observations}
        for index, signal in enumerate(report.signals):
            _check(
                signal.evidence,
                f"{case.category} 第 {index} 条信号没有依据",
            )
            unknown = [sid for sid in signal.evidence if sid not in known]
            _check(
                not unknown,
                f"{case.category} 第 {index} 条信号引用了不存在的观察 {unknown!r}",
            )

    report = _report_for(cases.HEALTHY_SAMPLE)
    context = build_feedback_context(report)
    _check(
        set(context.source_snapshot_ids)
        == {observation.snapshot_id for observation in report.observations},
        "进 checkpoint 的快照 id 与报告的观察对不上——事后回不到原始观察",
    )
    _check(
        context.snapshot_count == report.sample_count,
        "快照张数与样本量对不上",
    )
    _check(
        len(context.signals) <= MAX_CONTEXT_SIGNALS,
        f"进 checkpoint 的信号超过了上限 {MAX_CONTEXT_SIGNALS}",
    )
    _check(not context.is_empty, "有观察时上下文不该是空的")

    # 反证：把一条证据 id 改成一个不存在的值，出口必须拒绝。
    from app.feedback.report import _validate_signals

    broken = FeedbackSignal(
        kind=FeedbackSignalKind.ATTENTION,
        statement="观察到一条分布。这是观察到的分布，不构成任何因果结论。",
        metric="likes",
        value=1.0,
        sample_count=1,
        evidence=("不存在的快照",),
    )
    try:
        _validate_signals((broken,), report.observations)
    except ValueError:
        pass
    else:
        raise FeedbackSelfTestFailure(
            "引用了不存在的快照却没有被拒绝——可追溯性只是一句声明"
        )

    # 空输入不是"一条都没有依据的结论"，而是"没有结论"。
    empty = build_report(())
    _check(empty.sample_count == 0 and not empty.signals, "没有观察时不该产出信号")
    _check(empty.limitations, "没有观察时也必须把「没有」写进局限说明")
    _check(
        build_feedback_context(empty).is_empty,
        "没有观察时进 checkpoint 的上下文应当是空的",
    )
    return [
        "六组数据的每条信号依据都非空且真实存在；上下文的快照 id 与观察一一对应",
        "反证：引用不存在的快照 → 出口拒绝",
    ]


# ---------------------------------------------------------------------------
# 三、其余性质（成对反证）
# ---------------------------------------------------------------------------


def check_reports_are_deterministic() -> list[str]:
    """同一批观察两次，逐字段相同——包括措辞。

    措辞也一起比：数对得上而句子对不上，说明句子里有集合迭代顺序、
    挂钟时间之类的东西，而那正是"人拿着报告去复核"时会踩空的地方。
    """
    notes: list[str] = []
    for case in cases.CASES:
        first = _report_for(case)
        second = _report_for(case)
        _check(first == second, f"{case.category} 两次生成的报告不同")
        notes.append(f"{case.category}：{len(first.signals)} 条信号逐字段一致")
    return notes


def check_the_context_carries_nothing_but_the_summary() -> list[str]:
    """进 checkpoint 的上下文只带摘要，不带过去的正文（工作单第四十二节）。

    这是"反馈不是语料"的机器守卫：模型看到的必须是"我看过结果"，
    而不是任何一条过去帖子的原文。所以查两件事——`FeedbackContext` 的
    字段里没有可以装正文的地方，以及每段文本都短到不可能是正文。
    """
    from app.agent.prompts import MAX_FEEDBACK_CONTEXT_CHARS  # noqa: PLC0415

    context = build_feedback_context(build_report(cases.HEALTHY_SAMPLE.observations))
    fields = set(type(context).model_fields)
    _check(
        "observations" not in fields,
        "上下文里带上了 observations——那就是把快照明细塞进了 checkpoint",
    )
    for forbidden in ("content", "body", "text", "posts", "post_bodies"):
        _check(forbidden not in fields, f"上下文里出现了可能装正文的字段 `{forbidden}`")

    # 单条陈述比**整块**上下文给 Writer 的预算还长，那只可能是有人把正文
    # 或一整段观察记录拼了进来——模板句最长的一条也不到 200 字。
    for index, signal in enumerate(context.signals):
        _check(
            len(signal.statement) < MAX_FEEDBACK_CONTEXT_CHARS,
            f"第 {index} 条信号有 {len(signal.statement)} 字，比整块上下文的预算还长"
            "——那不像一条模板句",
        )
    # 反证：这个长度上限真的能发现一段正文。
    long_text = "正文" * MAX_FEEDBACK_CONTEXT_CHARS
    _check(
        len(long_text) >= MAX_FEEDBACK_CONTEXT_CHARS,
        "构造的长文本没有超过上限，这条反证什么都没证明",
    )
    return [
        f"上下文字段 {sorted(fields)}，没有一处可以装正文",
        "反证：超长文本会被长度上限发现",
    ]


#: 每一类的反向扰动：把触发它的那一项改回正常值，结论必须消失或翻过来。
#:
#: 这些扰动刻意写成**就地构造**而不是第七组数据：扰动往往只是改一个数字，
#: 写成数据会让 `cases.py` 里出现一堆"只为被改坏一次"的观察。
def _reversals() -> list[tuple[str, Callable[[], list[str]]]]:
    def small_sample() -> list[str]:
        case = cases.SMALL_SAMPLE
        raised = tuple(
            cases.obs(
                i,
                hours=float(i),
                likes=10 + i,
                replies=1,
                reposts=0,
                quotes=0,
            )
            for i in range(1, MIN_SAMPLES_FOR_COMPARISON + 1)
        )
        _check(len(case.observations) == 2, "这一组的前提是只有 2 条观察")
        kinds = _kinds(build_report(raised))
        _check(
            "insufficient_sample" not in kinds,
            f"补到下限之后仍有 insufficient_sample：{kinds}",
        )
        _check(
            "attention" in kinds,
            f"补到下限之后没有出现注意力信号：{kinds}",
        )
        return [f"2 → {MIN_SAMPLES_FOR_COMPARISON} 条观察：insufficient_sample 消失，attention 出现"]

    def healthy_sample() -> list[str]:
        case = cases.HEALTHY_SAMPLE
        reduced = case.observations[: MIN_SAMPLES_FOR_COMPARISON - 1]
        kinds = _kinds(build_report(reduced))
        _check(
            kinds == ("insufficient_sample",),
            f"把健康样本砍到下限以下之后应当只剩一条样本不足信号，实际 {kinds}",
        )
        return [f"8 → {MIN_SAMPLES_FOR_COMPARISON - 1} 条观察：四条分布信号塌缩为一条样本不足"]

    def missing_metric() -> list[str]:
        case = cases.MISSING_METRIC
        report = _report_for(case)
        _check(
            any(s.kind is FeedbackSignalKind.MISSING_METRIC for s in report.signals),
            "这一组的前提是有缺失信号",
        )
        filled = tuple(
            cases.obs(
                index,
                hours=observation.hours_since_publish,
                likes=observation.likes if observation.likes is not None else 1,
                replies=observation.replies,
                reposts=observation.reposts,
                quotes=observation.quotes,
            )
            for index, observation in enumerate(case.observations, start=1)
        )
        _check(
            not any(
                s.kind is FeedbackSignalKind.MISSING_METRIC for s in build_report(filled).signals
            ),
            "补齐读数之后缺失信号还在——它读的不是字段有没有值",
        )
        return ["补齐 3 条缺失读数：missing_metric 消失"]

    def multiple_windows() -> list[str]:
        case = cases.MULTIPLE_WINDOWS
        flat = tuple(
            cases.obs(
                index,
                hours=1.0,
                likes=observation.likes,
                replies=observation.replies,
                reposts=observation.reposts,
                quotes=observation.quotes,
            )
            for index, observation in enumerate(case.observations, start=1)
        )
        flat_notes = build_report(flat).limitations
        original = _report_for(case).limitations
        _check(
            flat_notes != original,
            "把全部观察压进同一档之后，覆盖说明一字未变——它读的不是小时数",
        )
        _check(
            any("本次观察覆盖的时间档" in note for note in flat_notes),
            "压平之后仍然应当有一条覆盖说明",
        )
        return ["8 条压进同一档：覆盖说明随之收窄"]

    def high_attention() -> list[str]:
        case = cases.HIGH_ATTENTION_LOW_CONVERSATION
        swapped = tuple(
            cases.obs(
                index,
                hours=float(index),
                likes=observation.replies + observation.quotes + 1,
                replies=observation.likes,
                reposts=observation.reposts,
                quotes=0,
            )
            for index, observation in enumerate(case.observations, start=1)
        )
        share = build_report(swapped).signals[3]
        _check(share.kind is FeedbackSignalKind.SHARE, "第 4 条信号应当是构成信号")
        _check(
            share.value is not None and share.value < 0.1,
            f"把注意力与对话对调之后构成应当翻过来，实际 {share.value}",
        )
        return [f"注意力与对话对调：构成 {_report_for(case).signals[3].value:.2f} → {share.value:.2f}"]

    def low_attention() -> list[str]:
        case = cases.LOW_ATTENTION_HIGH_CONVERSATION
        swapped = tuple(
            cases.obs(
                index,
                hours=float(index),
                likes=observation.replies + observation.quotes + 1,
                replies=1,
                reposts=observation.reposts,
                quotes=0,
            )
            for index, observation in enumerate(case.observations, start=1)
        )
        share = build_report(swapped).signals[3]
        _check(
            share.value is not None and share.value > 0.9,
            f"把对话与注意力对调之后构成应当翻过来，实际 {share.value}",
        )
        return [f"对话与注意力对调：构成 {_report_for(case).signals[3].value:.2f} → {share.value:.2f}"]

    return [
        ("small_sample", small_sample),
        ("healthy_sample", healthy_sample),
        ("missing_metric", missing_metric),
        ("multiple_windows", multiple_windows),
        ("high_attention_low_conversation", high_attention),
        ("low_attention_high_conversation", low_attention),
    ]


def check_every_case_survives_a_reverse_perturbation() -> list[str]:
    """六组数据各做一次反向扰动：结论必须跟着输入翻过来。

    只查"该响的响了"的基准，一个恒响的检测器也能全过。这条检查把每一类的
    关键变量**改回去**，并要求结论随之消失或翻转——翻不过来，说明检测器读的
    不是它声称读的那个变量。
    """
    notes: list[str] = []
    for name, reversal in _reversals():
        _check(name in CATEGORIES, f"扰动 {name} 不对应任何一类数据")
        notes.extend(reversal())
    _check(
        {name for name, _ in _reversals()} == set(CATEGORIES),
        "有类别没有配反向扰动——那一条就只剩「必须响」一条腿",
    )
    return notes


# ---------------------------------------------------------------------------

CHECKS: tuple[tuple[str, Any], ...] = (
    ("工作单点名的六类都在，且都有预期", check_the_six_categories_are_all_present),
    ("预期引用的字段与关系词都合法", check_expectations_reference_real_fields),
    ("六组数据全部满足各自的预期", check_every_case_matches_its_expectations),
    ("快照时间语义：分档只由存下来的小时数决定", check_snapshot_time_semantics),
    ("未知计数不是 0", check_null_metrics_not_zero),
    ("派生量的分母都说得清", check_derived_metric_requirements),
    ("低于样本下限就什么都不比", check_small_sample_guard),
    ("观察性措辞：无因果、无指令", check_causal_language_guard),
    ("可追溯性：没有依据就没有结论", check_feedback_traceability),
    ("同一输入 → 同一报告（逐字段）", check_reports_are_deterministic),
    ("进 checkpoint 的只有摘要，没有正文", check_the_context_carries_nothing_but_the_summary),
    ("每一类都配了反向扰动且都翻得过来", check_every_case_survives_a_reverse_perturbation),
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
            f"\nFeedback 自检：{len(CHECKS) - failures} 通过 / {failures} 失败"
            f"（共 {len(CHECKS)} 项）"
        )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/feedback/selftest.py",
        description="Feedback Loop Foundation 离线基准的自检（不碰 X、不碰 LLM）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条检查的说明")
    args = parser.parse_args(argv)
    return run_selftest(verbose=args.verbose, as_json=args.json)


if __name__ == "__main__":  # pragma: no cover - 入口
    sys.exit(main())


__all__ = [
    "CHECKS",
    "FeedbackSelfTestFailure",
    "run_selftest",
    "main",
]
