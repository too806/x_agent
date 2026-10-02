"""Learning Readiness 离线基准的自检。

跑法：

    python -m evals.learning.selftest

与 `evals/viewpoint/selftest.py` / `evals/feedback/selftest.py` 同一套纪律：断言
的方向是"**违规数据必须失败**"，而不是"干净数据应当通过"。每条检查都配一条
反证，因为一个恒真的守卫比没有守卫更糟——它让人以为那条禁令被守住了。

工作单第二十八节点名的五类误读，各自对应下面的一条检查：

    n=1 → 建议              check_the_gate_refuses_small_samples
    不同窗口 → 直接比较      check_different_windows_are_never_compared_directly
    NULL → 0                 check_null_is_never_counted_as_zero
    correlation → causation  check_no_causal_or_instructional_language
    小样本 → learning_ready  check_the_gate_refuses_small_samples

外加两条本层独有的、很容易被写坏的：

    分析单位             check_analysis_unit_is_the_post_not_the_snapshot
    "允许学习"≠"改 Agent"  check_learning_ready_still_cannot_modify_the_agent

全程离线：不碰 X、不碰 LLM、不读数据库、不取挂钟时间。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

from evals._bootstrap import force_utf8_stdout

from evals.learning import fixtures
from evals.learning.fixtures import WINDOW, Case

force_utf8_stdout()

from app.feedback.signals import (  # noqa: E402
    CAUSAL_MARKERS,
    INSTRUCTION_MARKERS,
    MIN_SAMPLES_FOR_COMPARISON,
    CausalLanguageError,
    InstructionalLanguageError,
    assert_no_causal_claim,
    assert_observational,
    find_causal_language,
    find_instruction_language,
)
from app.learning import cohort as cohort_module  # noqa: E402
from app.learning import evidence as evidence_module  # noqa: E402
from app.learning import performance as performance_module  # noqa: E402
from app.learning import report as report_module  # noqa: E402
from app.learning import runtime as runtime_module  # noqa: E402
from app.learning import schema as schema_module  # noqa: E402
from app.learning.cohort import build_cohorts  # noqa: E402
from app.learning.performance import (  # noqa: E402
    distribution,
    mark_outliers,
    percentile,
    select_window_observation,
    vector_at_window,
)
from app.learning.report import LearningReportError, build_learning_report  # noqa: E402
from app.learning.schema import (  # noqa: E402
    CohortDimension,
    LearningEvidenceKind,
    PerformanceMetric,
)


class LearningSelfTestFailure(AssertionError):
    """一条检查失败。与 `evals/selftest.py::SelfTestFailure` 同一角色。"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise LearningSelfTestFailure(message)


# ---------------------------------------------------------------------------
# 预期的求值
# ---------------------------------------------------------------------------

#: `fixtures.py` 里允许出现的关系词。多一个词就意味着多一处需要在这里实现的
#: 分支，所以它是一份**封闭**的表——出现表外的词直接判失败，而不是猜一个语义。
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
    "none_contain",
    "any_contain",
    "all_contain",
)

#: 报告层可以断言的全部字段。**封闭表**：拼错一个字段名要在这里报错，
#: 而不是在核对时抛一个看不出所以然的 AttributeError。
REPORT_FIELDS = (
    "ready",
    "reason",
    "sample_count",
    "post_count",
    "snapshot_count",
    "valid_comparison_count",
    "comparison_count",
    "evidence_count",
    "evidence_kinds",
    "automatic_modification",
    "failed_checks",
    "invalid_reasons",
    "all_statements",
    "attention_readings",
    "conversation_readings",
    "amplification_readings",
    "limitations",
)

_METRIC_FIELD = {
    "attention_readings": PerformanceMetric.ATTENTION,
    "conversation_readings": PerformanceMetric.CONVERSATION,
    "amplification_readings": PerformanceMetric.AMPLIFICATION,
}


def _readings(report: Any, metric: PerformanceMetric) -> int:
    """窗口内**有该指标读数**的帖子数。`None` 不计入——这是 NULL≠0 的落点。"""
    window = report.observation_window
    return sum(
        1
        for record in report.records
        if record.observation_window == window
        and record.performance.value_of(metric) is not None
    )


def _resolve(report: Any, path: str) -> Any:
    """从一份报告里取值。字段必须真的存在——拼错要报错，不能当默认值。"""
    if path not in REPORT_FIELDS:
        raise LearningSelfTestFailure(f"预期里写了 `{path}`，但它不是一个可断言的字段")
    readiness = report.readiness
    if path == "ready":
        return readiness.learning_ready
    if path == "reason":
        return readiness.reason
    if path == "sample_count":
        return readiness.sample_count
    if path == "automatic_modification":
        return readiness.automatic_modification
    if path == "failed_checks":
        return [check.name for check in readiness.checks if not check.passed]
    if path == "post_count":
        return len(report.records)
    if path == "snapshot_count":
        return sum(len(record.observations) for record in report.records)
    if path == "valid_comparison_count":
        return len(report.valid_comparisons)
    if path == "comparison_count":
        return len(report.comparisons)
    if path == "evidence_count":
        return len(report.evidence)
    if path == "evidence_kinds":
        return [item.type.value for item in report.evidence]
    if path == "invalid_reasons":
        return [c.invalid_reason or "" for c in report.comparisons if not c.valid]
    if path == "all_statements":
        return [c.statement for c in report.comparisons] + [
            e.statement for e in report.evidence
        ]
    if path == "limitations":
        return list(report.limitations)
    if path in _METRIC_FIELD:
        return _readings(report, _METRIC_FIELD[path])
    raise LearningSelfTestFailure(f"字段 `{path}` 没有实现")  # pragma: no cover


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
        # 与 `evals/feedback/selftest.py` 同一语义：字符串是**子串**，
        # 序列是**成员**。两种用例在预期里都出现过，靠实际类型区分。
        return expected in actual
    if relation == "not_contains":
        return expected not in actual
    if relation == "none_contain":
        return all(expected not in item for item in actual)
    if relation == "any_contain":
        return any(expected in item for item in actual)
    if relation == "all_contain":
        return all(expected in item for item in actual)
    if relation == "empty":
        return len(actual) == 0
    if relation == "nonempty":
        return len(actual) > 0
    raise LearningSelfTestFailure(f"不认识的关系词 `{relation}`")


def _report(case: Case) -> Any:
    return build_learning_report(case.records, window=case.window)


def _statement_case() -> Case:
    """一个"必然成立"的对照组：5 + 5、三指标齐、同一个窗口。

    多条检查都要一个**必定 ready** 的输入当反证的另一半。它是就地构造的，
    不进 `CASES`——`CASES` 是工作单点名的那九类，不多不少。
    """
    records = tuple(
        fixtures.make_record(
            i, observations=(fixtures.spec(20, 12, 6),), hook="question"
        )
        for i in range(5)
    ) + tuple(
        fixtures.make_record(
            i, observations=(fixtures.spec(4, 1, 0),), hook="statement"
        )
        for i in range(5, 10)
    )
    return Case(
        category="(ad-hoc)",
        note="就地构造的对照组",
        window=WINDOW,
        records=records,
        expect=[],
    )


# ---------------------------------------------------------------------------
# 一、基准数据本身
# ---------------------------------------------------------------------------


def check_the_nine_categories_are_all_present() -> list[str]:
    """工作单第二十七节点名的九类必须真的在，且每类都有记录与预期。

    "覆盖了九类"若只是文档里的一句话，它会一直是对的。这里让它是**可判的**：
    漏一类、多一类、加了一组没有预期的数据，都会红。

    同时挡住一种静默失效：一组数据带**空** `expect`，那么"逐条核对预期"
    那条检查会毫无意见地跳过它——看起来全绿，其实什么都没验。
    """
    present = tuple(case.category for case in fixtures.CASES)
    _check(
        present == fixtures.CATEGORIES,
        f"基准里的类别与工作单点名的不一致：{present} != {fixtures.CATEGORIES}",
    )
    _check(len(set(fixtures.CATEGORIES)) == len(fixtures.CATEGORIES), "类别名有重复")
    for case in fixtures.CASES:
        _check(case.records, f"{case.category} 没有任何记录")
        _check(case.expect, f"{case.category} 没有预期，等于没验")
        _check(case.window, f"{case.category} 没有说明观察窗口")
    return [
        f"{len(fixtures.CASES)} 类，共 {sum(len(c.expect) for c in fixtures.CASES)} 条预期"
    ]


def check_expectations_reference_real_fields() -> list[str]:
    """预期的字段名与关系词都必须合法，且每条都是四元组。"""
    seen: set[str] = set()
    count = 0
    for case in fixtures.CASES:
        for item in case.expect:
            _check(
                len(item) == 3,
                f"{case.category} 的预期 {item!r} 不是 (字段, 关系, 期望值)",
            )
            path, relation, _ = item
            _check(
                path in REPORT_FIELDS,
                f"{case.category} 的预期写了 `{path}`，它不是可断言的字段",
            )
            _check(
                relation in RELATIONS,
                f"{case.category} 的预期用了不认识的关系词 `{relation}`",
            )
            seen.add(path)
            count += 1
    return [f"{count} 条预期，涉及 {len(seen)} 个字段：{'、'.join(sorted(seen))}"]


def check_every_case_matches_its_expectations() -> list[str]:
    """九组数据逐条核对它们的预期。这是这份基准的主体。"""
    notes: list[str] = []
    for case in fixtures.CASES:
        report = _report(case)
        for path, relation, expected in case.expect:
            actual = _resolve(report, path)
            _check(
                _satisfies(actual, relation, expected),
                f"{case.category}：{path} {relation} {expected!r} 不成立（实际 {actual!r}）",
            )
        notes.append(f"{case.category}：{len(case.expect)} 条预期全部成立")
    return notes


# ---------------------------------------------------------------------------
# 二、确定性
# ---------------------------------------------------------------------------


def check_reports_are_bit_identical_across_runs() -> list[str]:
    """同一输入两次，逐字节相同——包括每一句陈述与每一条局限。"""
    for case in fixtures.CASES:
        first = json.dumps(_report(case).model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        second = json.dumps(_report(case).model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        _check(first == second, f"{case.category} 两次产出的报告不同")
    return [f"{len(fixtures.CASES)} 组两份报告逐字节一致"]


# ---------------------------------------------------------------------------
# 三、分析单位与窗口
# ---------------------------------------------------------------------------


def check_analysis_unit_is_the_post_not_the_snapshot() -> list[str]:
    """**本层最容易写坏的一条**：n 是帖子数，不是快照数。

    反证有两半：

    - 一条帖子 6 条快照 → 仍然是 n=1（把快照当样本，会立刻"够 5 条"）；
    - 5 条帖子各 1 条快照 → n=5（若按帖子去重去过头，会掉到 1）。
    """
    many_snapshots = fixtures.make_record(
        0, observations=tuple(fixtures.spec(10 + i, 1, 0) for i in range(6))
    )
    report = build_learning_report((many_snapshots,), window=WINDOW)
    _check(
        report.readiness.sample_count == 1,
        f"一条帖子 6 条快照被算成了 n={report.readiness.sample_count}，不是 1",
    )
    _check(_resolve(report, "snapshot_count") == 6, "快照数应当如实是 6")

    five = tuple(
        fixtures.make_record(i, observations=(fixtures.spec(10, 1, 0),))
        for i in range(5)
    )
    five_report = build_learning_report(five, window=WINDOW)
    _check(
        five_report.readiness.sample_count == 5,
        f"5 条帖子被算成了 n={five_report.readiness.sample_count}，不是 5",
    )
    return [
        "1 帖 6 快照 → n=1（快照不是样本）",
        "5 帖 1 快照 → n=5（帖子不会被重复去重）",
    ]


def check_different_windows_are_never_compared_directly() -> list[str]:
    """窗口不一致时：不比较、不产出方向结论，且 cohort 不谎报自己的窗口。"""
    case = fixtures.case_for("different_windows")
    report = _report(case)
    _check(not report.readiness.learning_ready, "跨窗口的记录被当成了可以学习的数据")
    _check(
        report.readiness.reason == "inconsistent_window",
        f"reason 应当是 inconsistent_window，实际 {report.readiness.reason!r}",
    )
    _check(not report.valid_comparisons, "跨窗口的报告里出现了成立的比较")
    for statement in _resolve(report, "all_statements"):
        for marker in ("更高", "更低", "高于", "低于"):
            _check(
                marker not in statement,
                f"跨窗口的报告里出现了方向性结论（{marker}）：{statement!r}",
            )

    # 另一半反证：cohort 的 `observation_window` 是对它**全部成员**的陈述，
    # 所以窗口不符的记录一条都不许进组。
    for dimension in (CohortDimension.HOOK_TYPE, CohortDimension.LENGTH_BUCKET):
        for group in build_cohorts(case.records, dimension, window=WINDOW):
            member_ids = set(group.post_ids)
            wrong = [
                r.published_post_id
                for r in case.records
                if r.published_post_id in member_ids and r.observation_window != WINDOW
            ]
            _check(
                not wrong,
                f"cohort {group.value!r} 里混进了别的窗口的记录：{wrong!r}",
            )

    # 反证第三半：同一个窗口的记录照样能成立，否则"拒绝"可能只是碰巧。
    healthy = _report(_statement_case())
    _check(healthy.readiness.learning_ready, "同窗口的对照组反而没通过，说明拒绝的不是窗口")
    return [
        "跨窗口 → 不成立、无方向结论、无成员谎报窗口",
        "反证：同窗口的对照组照常成立",
    ]


# ---------------------------------------------------------------------------
# 四、NULL 与离群点
# ---------------------------------------------------------------------------


def check_null_is_never_counted_as_zero() -> list[str]:
    """读不到的东西**不进分布**：既不补 0，也不算一个读数。"""
    case = fixtures.case_for("missing_metrics")
    report = _report(case)
    for field in ("attention_readings", "conversation_readings", "amplification_readings"):
        count = _resolve(report, field)
        _check(count == 2, f"{field} 应当是 2（各自真有读数的帖子数），实际 {count}")
        _check(count < len(report.records), f"{field} 把没有读数的帖子也算进去了")

    # 逐条核到一个具体的帖子上：likes=None 的那条帖子，attention 必须是 None。
    no_likes = fixtures.make_record(0, observations=(fixtures.spec(conversation=4),))
    _check(no_likes.performance.attention is None, "读不到的 likes 变成了一个值")
    _check(no_likes.performance.conversation == 4, "有读数的 conversation 反而丢了")

    # 反证：一个**真的** 0 必须被当成 0。
    zero = fixtures.make_record(0, observations=(fixtures.spec(0, 0, 0),))
    _check(zero.performance.attention == 0, "真的读到 0 被当成了缺失")
    _check(
        distribution([0.0]).median == 0.0,
        "只有一条 0 读数的分布，中位数应当是 0，不是 None",
    )
    _check(distribution([]).median is None, "空分布的 median 应当是 None")
    return [
        "稀疏指标各自只算真有读数的帖子",
        "反证：读到的 0 仍然是 0；没有任何读数时是 None",
    ]


def check_percentiles_and_outliers_are_deterministic() -> list[str]:
    """分位数用线性插值；离群点只标记，**绝不删除**。"""
    _check(percentile([1, 2, 3, 4], 0.5) == 2.5, "median([1,2,3,4]) 应当是 2.5")
    _check(percentile([1, 2, 3, 4], 0.25) == 1.75, "p25([1,2,3,4]) 应当是 1.75")
    _check(percentile([1, 2, 3, 4], 0.75) == 3.25, "p75([1,2,3,4]) 应当是 3.25")
    _check(percentile([7], 0.5) == 7.0, "单点序列的分位数应当是它自己")
    _check(percentile([], 0.5) is None, "空序列的分位数应当是 None")

    values = [10.0, 11.0, 12.0, 13.0, 900.0]
    flags = mark_outliers(values)
    _check(len(flags) == len(values), "离群标记与输入没有一一对应")
    _check(flags[-1] is True, "900 没有被标成离群")
    _check(sum(flags) == 1, f"只有 900 该被标记，实际标了 {sum(flags)} 个")
    _check(mark_outliers([1.0, 2.0, 3.0]) == (False, False, False), "读数太少时不该判离群")

    dist = distribution(values)
    _check(dist.sample_count == 5, "离群点被从分布里删掉了")
    _check(dist.median == 12.0, f"中位数应当是 12，实际 {dist.median}")
    _check(dist.mean is not None and dist.mean > 100, "均值应当被那个离群点拉走")
    return [
        f"离群被标记 {sum(flags)} 个，仍参与计算：n={dist.sample_count}，中位数 {dist.median}",
        "反证：n<4 时不判离群",
    ]


def check_the_outlier_case_matches_its_own_expectations() -> list[str]:
    """`outlier` 这一组：中位数不被一个爆款带走，但爆款也没被丢掉。"""
    report = _report(fixtures.case_for("outlier"))
    attention = [
        c
        for c in report.valid_comparisons
        if c.dimension is CohortDimension.HOOK_TYPE
        and c.metric is PerformanceMetric.ATTENTION
    ]
    _check(len(attention) == 1, "outlier 组里按 hook_type 的注意力比较没成立")
    comparison = attention[0]
    group_a = comparison.group_a
    _check(group_a.outlier_count == 1, f"离群点没被标记（{group_a.outlier_count}）")
    _check(group_a.sample_count == 5, "离群点被从样本里剔除了")
    _check(group_a.maximum == 900.0, "最大值不是那条 900 的观察")
    _check(group_a.median == 12.0, f"中位数被离群点带走了（{group_a.median}）")
    # 两组中位数相等，所以这里**不该**出现方向性结论——离群点不该换来一个结论。
    for marker in ("更高", "更低"):
        _check(
            marker not in comparison.statement,
            f"中位数相等却给出了方向性结论：{comparison.statement!r}",
        )
    _check("中位数相同" in comparison.statement, "两组中位数相等这件事没有被说出来")
    return [
        f"n={group_a.sample_count} 全部保留，最大值 {group_a.maximum}，中位数 {group_a.median}",
        "反证：中位数相等时不给方向结论",
    ]


# ---------------------------------------------------------------------------
# 五、闸门
# ---------------------------------------------------------------------------


def check_the_gate_refuses_small_samples() -> list[str]:
    """工作单第二十八节：n 太小既不能产出结论，也不能 `learning_ready`。"""
    for category in ("one_post", "small_sample"):
        case = fixtures.case_for(category)
        report = _report(case)
        _check(not report.readiness.learning_ready, f"{category} 在小样本上 ready 了")
        _check(
            report.readiness.reason == "insufficient_sample",
            f"{category} 的 reason 应当是 insufficient_sample，实际 {report.readiness.reason!r}",
        )
        _check(not comparison_has_conclusions(report), f"{category} 产出了差异结论")
        _check(
            any(
                item.type is LearningEvidenceKind.INSUFFICIENT_SAMPLE
                for item in report.evidence
            ),
            f"{category} 没有留下一条 insufficient_sample 的证据",
        )
        for statement in _resolve(report, "all_statements"):
            for marker in ("更高", "更低", "建议", "应当"):
                _check(
                    marker not in statement,
                    f"{category} 的陈述里出现了结论性措辞（{marker}）：{statement!r}",
                )

    # 反证：样本量刚够的同构数据必须通过——否则"拒绝"可能只是恒真。
    healthy = _report(_statement_case())
    _check(
        healthy.readiness.sample_count == MIN_SAMPLES_FOR_COMPARISON * 2,
        "对照组本身的样本量就不对",
    )
    _check(healthy.readiness.learning_ready, "样本量充足时闸门没有打开")
    return [
        "one_post（n=1）与 small_sample（n=4）都被拒绝，且无一句结论",
        "反证：n=10 的同构数据通过",
    ]


def comparison_has_conclusions(report: Any) -> bool:
    """报告里有没有"差异结论"。成立的比较即算——不论方向是否被写出来。"""
    return bool(report.valid_comparisons)


def check_learning_ready_still_cannot_modify_the_agent() -> list[str]:
    """工作单第二十五、三十六节：**没有** `apply_learning()`，一个都不许有。

    这条用一个 AST 扫描来钉死，而不是 `hasattr`：`hasattr` 只看得见模块属性，
    一个定义在函数里的局部函数、或一个还没被 import 的子模块，它都看不见。
    """
    package = Path(runtime_module.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("apply_"):
                    offenders.append(f"{path.name}::{node.name}")
    _check(not offenders, f"学习层里出现了自动改写的入口：{offenders!r}")

    for module in (
        cohort_module,
        evidence_module,
        performance_module,
        report_module,
        runtime_module,
        schema_module,
    ):
        apply_names = [name for name in dir(module) if name.startswith("apply_")]
        _check(not apply_names, f"{module.__name__} 里有 {apply_names!r}")

    # 允许"作为学习材料"不等于允许改 Agent：ready 的报告里也必须恒假。
    for case in fixtures.CASES:
        report = _report(case)
        _check(
            report.readiness.automatic_modification is False,
            f"{case.category} 的报告里 automatic_modification 不是 False",
        )
        if report.readiness.learning_ready:
            _check(
                report.readiness.usable_for_future_learning is True,
                f"{case.category}：ready 与 usable_for_future_learning 不一致",
            )
    return [
        f"扫描 {len(list(package.glob('*.py')))} 个模块，没有任何 apply_* 入口",
        "全部报告 automatic_modification=False",
    ]


def check_the_gate_is_a_code_decision_not_a_model_call() -> list[str]:
    """闸门由显式布尔推出，四条判据必须**同时**成立。

    反证逐条做：拿一个 ready 的报告，逐个把判据对应的输入改坏，
    `learning_ready` 必须跟着翻成 False——一个恒真的闸门在这里过不去。
    """
    case = _statement_case()
    report = _report(case)
    _check(report.readiness.learning_ready, "对照组本身没通过")
    names = [check.name for check in report.readiness.checks]
    _check(
        names == ["sample_count", "window_consistent", "metrics_present", "comparison_valid"],
        f"四条判据的名字或顺序变了：{names}",
    )

    # 反证：把其中一组砍到只剩 2 条帖子 → 比较不成立 → 整体不再 ready。
    fewer = tuple(
        fixtures.make_record(
            i, observations=(fixtures.spec(20, 12, 6),), hook="question"
        )
        for i in range(5)
    ) + tuple(
        fixtures.make_record(
            i, observations=(fixtures.spec(4, 1, 0),), hook="statement"
        )
        for i in range(5, 8)
    )
    degraded = build_learning_report(fewer, window=WINDOW)
    _check(
        not degraded.readiness.learning_ready,
        "把一组砍到 3 条之后闸门还开着——它没有真的在读比较结果",
    )
    _check(
        degraded.readiness.reason == "invalid_comparison",
        f"reason 应当是 invalid_comparison，实际 {degraded.readiness.reason!r}",
    )
    return [
        "四条判据齐全且顺序固定",
        "反证：一组砍到 3 条 → invalid_comparison",
    ]


# ---------------------------------------------------------------------------
# 六、措辞与可追溯
# ---------------------------------------------------------------------------


def check_no_causal_or_instructional_language() -> list[str]:
    """工作单第二十节：结论是观察，不是因果，也不是指令。

    三半反证：

    1. 全部九组的每一句话都过因果词与指令词检查；
    2. 守卫**真的会响**——手写的因果句/指令句必须抛错（否则第 1 条是空的）；
    3. 出口校验真的在拦——把一句因果话塞进报告，`_validate` 必须抛
       `LearningReportError`。
    """
    statements = 0
    for case in fixtures.CASES:
        report = _report(case)
        for statement in _resolve(report, "all_statements"):
            statements += 1
            assert_no_causal_claim(statement, where=f"{case.category} 陈述")
            hits = find_instruction_language(statement)
            _check(not hits, f"{case.category} 的陈述里有指令词 {hits!r}：{statement!r}")

    try:
        assert_no_causal_claim("因为开头是问句所以点赞更多。", where="注入")
    except CausalLanguageError:
        pass
    else:  # pragma: no cover - 守卫失效
        raise LearningSelfTestFailure("因果守卫对一句明显的因果话没有反应")

    try:
        assert_observational("你应该多用问句开头。", where="注入")
    except InstructionalLanguageError:
        pass
    else:  # pragma: no cover - 守卫失效
        raise LearningSelfTestFailure("观察性守卫对一句明显的指令话没有反应")

    report = _report(_statement_case())
    poisoned = report.model_copy(
        update={
            "comparisons": tuple(
                comparison.model_copy(
                    update={"statement": "因为用了问句，所以互动更高。"}
                )
                for comparison in report.comparisons
            )
        }
    )
    try:
        report_module._validate(poisoned)  # noqa: SLF001 - 被测的就是这道出口校验
    except LearningReportError:
        pass
    else:  # pragma: no cover - 出口校验失效
        raise LearningSelfTestFailure("出口校验放过了一句因果结论")

    _check(
        not find_causal_language("在 24-72h 窗口下观察到更高的中位数。"),
        "干净的观察句被判成了因果句",
    )
    _check(len(CAUSAL_MARKERS) > 5 and len(INSTRUCTION_MARKERS) > 5, "标记表疑似被清空")
    return [
        f"{statements} 句陈述全部通过因果/指令检查",
        "反证：注入因果句与指令句都会抛错，出口校验也会拦",
    ]


def check_every_evidence_is_traceable() -> list[str]:
    """工作单第十八节：证据 → 快照 → 帖子 → x_post_id 一路可追。"""
    traced = 0
    for case in fixtures.CASES:
        report = _report(case)
        known_snapshots = {
            o.snapshot_id for record in report.records for o in record.observations
        }
        known_posts = {record.published_post_id for record in report.records}
        known_x = {record.x_post_id for record in report.records}
        for item in report.evidence:
            _check(bool(item.source_snapshot_ids), f"{case.category}：证据 {item.id} 没有依据")
            unknown = [s for s in item.source_snapshot_ids if s not in known_snapshots]
            _check(not unknown, f"{case.category}：证据 {item.id} 引用了不存在的观察 {unknown!r}")
            unknown_posts = [p for p in item.source_post_ids if p not in known_posts]
            _check(not unknown_posts, f"{case.category}：证据 {item.id} 引用了不存在的帖子")
            unknown_x = [x for x in item.source_x_post_ids if x not in known_x]
            _check(not unknown_x, f"{case.category}：证据 {item.id} 的 x_post_id 追不到")
            _check(item.confidence is None, f"{case.category}：证据 {item.id} 编了一个 confidence")
            traced += 1
    return [f"{traced} 条证据全部可追溯，且 confidence 一律为 None"]


def check_invalid_comparisons_are_shown_not_dropped() -> list[str]:
    """工作单第三十五节：哪里不足要摆出来，不能静默丢掉一个比较。"""
    shown = 0
    for case in fixtures.CASES:
        report = _report(case)
        for comparison in report.comparisons:
            if not comparison.valid:
                _check(
                    comparison.invalid_reason,
                    f"{case.category}：不成立的比较没有写原因",
                )
                _check(
                    "不构成可比" in comparison.statement,
                    f"{case.category}：不成立的比较却在陈述里说得像个结论",
                )
                shown += 1
        if report.comparisons:
            _check(
                len(report.comparisons) == len(report.valid_comparisons) + shown_in(report),
                f"{case.category}：比较数与成立数对不上",
            )
    return [f"{shown} 条不成立的比较都带着原因出现在报告里"]


def shown_in(report: Any) -> int:
    return sum(1 for comparison in report.comparisons if not comparison.valid)


def check_ready_reports_still_never_tell_the_writer_what_to_do() -> list[str]:
    """`ready` 的报告能进 Writer 上下文，但它说的仍然是观察。

    这一段是接口层（`format_learning_context`）——本阶段**没有**接进任何模板，
    所以这里同时钉住"它有话说"与"它没有指令"。
    """
    from app.agent.prompts import (  # noqa: PLC0415 - 接口层，按需 import
        MAX_LEARNING_CONTEXT_CHARS,
        format_learning_context,
    )

    ready = format_learning_context(_report(_statement_case()))
    _check(ready, "ready 的报告渲染成了空串")
    _check(len(ready) <= MAX_LEARNING_CONTEXT_CHARS + 32, "学习上下文没有受长度上限约束")
    for marker in INSTRUCTION_MARKERS:
        _check(marker not in ready, f"学习上下文里出现了指令词 {marker!r}")
    for marker in CAUSAL_MARKERS:
        _check(marker not in ready, f"学习上下文里出现了因果词 {marker!r}")

    empty = format_learning_context(None)
    _check(empty, "没有报告时应当有一句交代，而不是空串")
    for marker in INSTRUCTION_MARKERS + CAUSAL_MARKERS:
        _check(marker not in empty, f"空报告的交代里有 {marker!r}")

    denied = format_learning_context(_report(fixtures.case_for("one_post")))
    _check("尚不具备" in denied, "样本不足的报告应当明说还不具备学习条件")
    for marker in ("建议", "应当", "应该"):
        _check(marker not in denied, f"样本不足的报告里出现了结论性措辞 {marker!r}")
    return [
        f"ready 报告渲染 {len(ready)} 字，无因果/指令词",
        "反证：None 与样本不足两条分支也都有话说、且不含指令",
    ]


# ---------------------------------------------------------------------------
# 七、Phase 5D：提案层与这一层的关系
# ---------------------------------------------------------------------------
#
# 提案基准（`evals/learning/proposals.py`）自查它那八类；这里补的是**跨层**的
# 那几条——提案基准再怎么全，也答不了"提案引用的证据是不是真的来自这份报告"
# 和"提案层有没有偷偷写库"，因为那要同时看两层。


def check_the_proposal_benchmark_is_green() -> list[str]:
    """提案基准的每一项检查都必须过。

    把它的检查**跑一遍**，而不是 import 一下就算数：只 import 的话，
    提案基准里一条永远抛错的检查会让这里绿着过去。
    """
    from evals.learning import proposals as proposal_benchmark  # noqa: PLC0415

    _check(
        tuple(proposal_benchmark.PROPOSAL_CATEGORIES)
        == proposal_benchmark.PROPOSAL_CATEGORIES,
        "提案基准的类别清单不是元组",
    )
    _check(
        len(proposal_benchmark.PROPOSAL_CATEGORIES) == 8,
        f"工作单第二十六节点名八类，实际 "
        f"{len(proposal_benchmark.PROPOSAL_CATEGORIES)} 类",
    )
    notes: list[str] = []
    for name, check in proposal_benchmark.CHECKS:
        check()  # 抛错就是失败，异常类型由 run_selftest 统一呈现
        notes.append(name)
    return [f"提案基准 {len(notes)} 项全过，覆盖 8 类场景"]


def check_every_proposal_traces_back_to_its_evidence() -> list[str]:
    """工作单第八节：提案引用的每一条证据，都必须是**这份报告**里真有的。

    提案基准断言的是"提案自己前后一致"；这里断言的是"提案和你给它的那份报告
    一致"——两者的差别正是"证据是不是编的"。

    另外核一遍 `sample_count`：它必须是那几条证据对应比较的**帖子并集**。
    照抄提案基准的算法没有意义，所以这里直接用 `report.evidence` 与
    `report.comparisons` 重算。
    """
    from evals.learning import proposals as proposal_benchmark  # noqa: PLC0415
    from app.learning.evidence import evidence_id  # noqa: PLC0415

    checked = 0
    for category in ("balanced_cohorts", "causal_language"):
        report = build_learning_report(
            fixtures.case_for(category).records, window=WINDOW
        )
        known = {item.id: item for item in report.evidence}
        by_id = {
            evidence_id(comparison): comparison
            for comparison in report.comparisons
            if comparison.valid
        }
        proposals = proposal_benchmark.generate_proposals(
            report, base_recipe=proposal_benchmark.base_recipe()
        )
        for proposal in proposals:
            checked += 1
            _check(proposal.evidence_ids, f"{category}：提案 {proposal.id} 没有证据")
            for item_id in proposal.evidence_ids:
                _check(
                    item_id in known,
                    f"{category}：提案 {proposal.id} 引用了这份报告里没有的证据 "
                    f"{item_id!r}",
                )
                _check(
                    known[item_id].source_snapshot_ids,
                    f"{category}：证据 {item_id} 本身没有来源快照",
                )
            post_ids: set[str] = set()
            for item_id in proposal.evidence_ids:
                comparison = by_id.get(item_id)
                _check(
                    comparison is not None,
                    f"{category}：提案 {proposal.id} 的证据 {item_id!r} "
                    "对不上一份成立的比较",
                )
                post_ids.update(comparison.group_a.post_ids)
                post_ids.update(comparison.group_b.post_ids)
            _check(
                proposal.sample_count == len(post_ids),
                f"{category}：提案 {proposal.id} 说 n={proposal.sample_count}，"
                f"但证据涉及的帖子并集是 {len(post_ids)} 条",
            )
            _check(
                proposal.sample_count <= report.readiness.sample_count,
                f"{category}：提案的样本量超过了报告本身的样本量",
            )
    _check(checked > 0, "没有任何提案被核到，这条检查是空的")
    return [f"{checked} 条提案的证据全部可追回报告，且样本量等于帖子并集"]


def check_the_proposal_layer_reuses_the_same_threshold() -> list[str]:
    """工作单第十八节：阈值只有**一个**，不许提案层自备一个更松的。

    两半：两个模块里的常量必须是同一个对象；以及闸门关着的数据在提案层
    仍然产出零条——后者若不做，"复用常量"完全可以只是共享了一个没人读的数字。
    """
    from app.learning import proposal as proposal_module  # noqa: PLC0415

    _check(
        proposal_module.MIN_SAMPLES_FOR_COMPARISON is MIN_SAMPLES_FOR_COMPARISON,
        "提案层没有复用 5C 的 MIN_SAMPLES_FOR_COMPARISON，而是另立了一个",
    )
    _check(
        MIN_SAMPLES_FOR_COMPARISON == 5,
        f"阈值被改成了 {MIN_SAMPLES_FOR_COMPARISON}；工作单要求原样保留 5",
    )
    from evals.learning import proposals as proposal_benchmark  # noqa: PLC0415

    for category in proposal_benchmark.GATE_CLOSED_CATEGORIES.values():
        report = build_learning_report(fixtures.case_for(category).records, window=WINDOW)
        proposals = proposal_benchmark.generate_proposals(
            report, base_recipe=proposal_benchmark.base_recipe()
        )
        _check(
            not proposals,
            f"{category}：样本不足却产出了 {len(proposals)} 条提案",
        )
    return [
        f"阈值是同一条常量（{MIN_SAMPLES_FOR_COMPARISON}）",
        "反证：三类样本不足的数据在提案层都是零条",
    ]


#: `session` 这一类接收者上调用的、属于写操作的方法名。
_SESSION_WRITES = frozenset(
    {"add", "add_all", "commit", "delete", "execute", "flush", "merge", "rollback"}
)
#: SQLAlchemy 的写构造器；它们在代码里通常是以**裸名**被调用的
#: （`from sqlalchemy.dialects.postgresql import insert`）。
_SQL_WRITE_CALLS = frozenset({"insert", "update", "delete", "pg_insert", "upsert"})


def _scan_for_writes(source: str, label: str) -> list[str]:
    """从一段源码里找出"写库"的调用。

    判定比"名字里带 update"严得多：`post_ids.update(x)` 是本地集合的更新，
    与数据库毫无关系。所以只有当

    * 接收者名字里带 `session`（`session.add(...)`、`self._session.execute(...)`），或
    * 被调用的是一个裸名写构造器（`insert(...)`、`pg_insert(...)`）

    时才算数。第一版把前者放宽成"任何 Name 接收者"，结果 `set.update()`
    被当成了写库——一个会误报的守卫最后一定会被人关掉。
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _SQL_WRITE_CALLS:
                offenders.append(f"{label}:{node.lineno} {func.id}()")
            continue
        if not isinstance(func, ast.Attribute) or func.attr not in _SESSION_WRITES:
            continue
        receiver = func.value
        while isinstance(receiver, ast.Attribute):  # self._session.add(...)
            receiver = receiver.value
        if isinstance(receiver, ast.Name) and "session" in receiver.id.lower():
            offenders.append(f"{label}:{node.lineno} {receiver.id}.{func.attr}()")
    return offenders


def check_the_learning_package_never_writes() -> list[str]:
    """工作单第十六节：提案层**完全独立**——它不写任何东西。

    用 AST 扫描写操作的**调用**，而不是搜字符串：搜字符串会把注释和文档里
    提到的 `update()` 也算上，而 AST 只看真正被调用到的方法。

    读取是允许的（`runtime.py` 要从库里读快照和裁决），写入不是。
    """
    # 反证：扫描器必须先证明**自己看得见**一次真的写。否则"没找到"
    # 既可以解释成"代码干净"，也可以解释成"这个正则从来就没匹配过"。
    planted = "async def f(session):\n    await session.add(row)\n    await session.commit()\n"
    _check(
        len(_scan_for_writes(planted, "(植入)")) == 2,
        "扫描器看不见一次植入的写操作，这条检查是空的",
    )
    _check(
        not _scan_for_writes("x = set()\nx.update([1])\n", "(反证)"),
        "扫描器把本地集合的 update 当成了写库",
    )

    package = Path(runtime_module.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        offenders.extend(
            _scan_for_writes(path.read_text(encoding="utf-8"), path.name)
        )
    _check(not offenders, f"学习层里出现了写操作：{offenders!r}")
    return [
        f"扫描 {len(list(package.glob('*.py')))} 个模块，没有任何写调用",
        "反证：植入的 session.add/commit 会被抓到，集合的 update 不会误报",
    ]


# ---------------------------------------------------------------------------

CHECKS: tuple[tuple[str, Any], ...] = (
    ("工作单点名的九类都在，且都有预期", check_the_nine_categories_are_all_present),
    ("预期引用的字段与关系词都合法", check_expectations_reference_real_fields),
    ("九组数据全部满足各自的预期", check_every_case_matches_its_expectations),
    ("同一输入 → 同一份报告（逐字节）", check_reports_are_bit_identical_across_runs),
    ("分析单位是帖子，不是快照（成对反证）", check_analysis_unit_is_the_post_not_the_snapshot),
    ("不同窗口从不直接比较（反证）", check_different_windows_are_never_compared_directly),
    ("NULL 从不被当成 0（反证）", check_null_is_never_counted_as_zero),
    ("分位数与离群点：只标记、不删除（反证）", check_percentiles_and_outliers_are_deterministic),
    ("outlier 那一组：中位数不动，爆款不丢", check_the_outlier_case_matches_its_own_expectations),
    ("小样本既不产结论也不 ready（反证）", check_the_gate_refuses_small_samples),
    ("闸门是代码判定，四条判据缺一不可（反证）", check_the_gate_is_a_code_decision_not_a_model_call),
    ("没有因果、没有指令（注入反证）", check_no_causal_or_instructional_language),
    ("每条证据都能追到快照/帖子/x_post_id", check_every_evidence_is_traceable),
    ("不成立的比较摆出来，不静默丢弃", check_invalid_comparisons_are_shown_not_dropped),
    ("ready 也不告诉 Writer 该怎么做", check_ready_reports_still_never_tell_the_writer_what_to_do),
    ("学习层没有 apply_learning（AST 扫描）", check_learning_ready_still_cannot_modify_the_agent),
    # --- Phase 5D ---
    ("提案基准（第二十六节八类）全绿", check_the_proposal_benchmark_is_green),
    ("每条提案都能追回报告里的证据（反证）", check_every_proposal_traces_back_to_its_evidence),
    ("提案层复用同一个阈值，不自备一个（反证）", check_the_proposal_layer_reuses_the_same_threshold),
    ("提案层不写任何东西（AST 扫描）", check_the_learning_package_never_writes),
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
            f"\nLearning 自检：{len(CHECKS) - failures} 通过 / {failures} 失败"
            f"（共 {len(CHECKS)} 项）"
        )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/learning/selftest.py",
        description="Learning Readiness 离线基准的自检（不碰 X、不碰 LLM、不碰数据库）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条检查的说明")
    args = parser.parse_args(argv)
    return run_selftest(verbose=args.verbose, as_json=args.json)


if __name__ == "__main__":  # pragma: no cover - 入口
    sys.exit(main())


__all__ = [
    "CHECKS",
    "LearningSelfTestFailure",
    "main",
    "run_selftest",
]
