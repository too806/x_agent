"""Learning Evidence 与确定性 learning gate（Phase 5C 工作单第十八、十九、二十四节）。

## 这一层唯一的功能：把"够不够"变成一个**代码判定**

工作单第二十四节要求 `learning_gate` 是确定性的，**不让 LLM 决定这个开关**。
所以这里的每一个判断都是显式的布尔，来自可核对的计数：

    sample_count       窗口内有多少条帖子真有读数
    window consistent  所有记录是不是同一个观察窗口
    metrics present    窗口里三类计数合计够不够
    comparison valid   有没有至少一组 cohort 比较成立

四条**同时**成立才 `learning_ready=True`。任何一条不成立，`reason` 就指向它，
并且报告里出现的是一条"什么也没法说"的证据，而不是一个空结果。

## `learning_ready=True` 只表示"可以作为未来学习材料"

它**不**表示"立即改变 Agent"（工作单第二十五节）。所以：

- `LearningReadiness.automatic_modification` 恒为 `False`；
- 本模块（乃至整个 `app/learning/`）**没有** `apply_learning()`；
  `evals/learning/selftest.py` 用一条断言把这个缺失钉死——将来有人加回来，
  测试会先失败。

## 证据永远可追溯

每一条证据都带着 `source_snapshot_ids` / `source_post_ids` / `source_x_post_ids`，
沿 `comparison → post → snapshot → x_post` 一路能回到原始观察（工作单第十八节）。
`app/learning/report.py` 在出口处再核验一遍这些 id 真的出现在报告里。
"""

from __future__ import annotations

from typing import Sequence

from app.feedback.signals import MIN_SAMPLES_FOR_COMPARISON
from app.learning.performance import select_window_observation
from app.learning.schema import (
    CohortDefinition,
    LearningEvidence,
    LearningEvidenceKind,
    LearningReadiness,
    ObservedComparison,
    PerformanceMetric,
    PostPerformanceRecord,
    ReadinessCheck,
)

#: 三类互动（表现向量的三个分量）。
_ALL_METRICS: tuple[PerformanceMetric, ...] = (
    PerformanceMetric.ATTENTION,
    PerformanceMetric.CONVERSATION,
    PerformanceMetric.AMPLIFICATION,
)

#: readiness 的 reason → 对应的"什么也没法说"的证据种类。
_REASON_KINDS: dict[str, LearningEvidenceKind] = {
    "insufficient_sample": LearningEvidenceKind.INSUFFICIENT_SAMPLE,
    "inconsistent_window": LearningEvidenceKind.WINDOW_MISMATCH,
    "missing_metrics": LearningEvidenceKind.MISSING_METRIC,
    "invalid_comparison": LearningEvidenceKind.INVALID_COMPARISON,
}


def _slug(text: str) -> str:
    """把一段取值压成一个 id 片段：非字母数字一律折成 `-`。

    id 必须**确定性**——同一组比较每次得到同一个 id，否则"这条证据上次也在"
    这件事无法核对。中文会被折掉，于是 `a`/`b` 退化成一对 `-`，但加上维度、
    窗口、指标与序号后仍然唯一。可读性由 `comparison_group` 承担，不由 id 承担。
    """
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() else "-")
    collapsed = "".join(out)
    while "--" in collapsed:
        collapsed = collapsed.replace("--", "-")
    return collapsed.strip("-") or "x"


def evidence_id(comparison: ObservedComparison) -> str:
    """一条比较证据的确定性 id。"""
    return "learning:" + ":".join(
        [
            _slug(comparison.dimension.value),
            _slug(comparison.group_a.value),
            _slug(comparison.group_b.value),
            _slug(comparison.observation_window),
            _slug(comparison.metric.value),
        ]
    )


def _x_post_ids(
    records: Sequence[PostPerformanceRecord], post_ids: Sequence[str]
) -> tuple[str, ...]:
    by_id = {r.published_post_id: r for r in records}
    return tuple(
        sorted(
            {
                by_id[pid].x_post_id
                for pid in post_ids
                if pid in by_id and by_id[pid].x_post_id
            }
        )
    )


def _comparison_evidence(
    comparison: ObservedComparison, records: Sequence[PostPerformanceRecord]
) -> LearningEvidence:
    post_ids = tuple(sorted(set(comparison.group_a.post_ids) | set(comparison.group_b.post_ids)))
    return LearningEvidence(
        id=evidence_id(comparison),
        type=LearningEvidenceKind.COHORT_DIFFERENCE,
        statement=comparison.statement,
        # 两组读数条数之和；每一组各自的 n 写在 statement 里，不在这里拆成两个字段
        # （一个"n"字段装两个数只会让下游各自解释）。
        sample_count=comparison.group_a.sample_count + comparison.group_b.sample_count,
        comparison_group=comparison.comparison_group,
        observation_window=comparison.observation_window,
        metric=comparison.metric.value,
        dimension=comparison.dimension.value,
        source_snapshot_ids=comparison.source_snapshot_ids,
        source_post_ids=post_ids,
        source_x_post_ids=_x_post_ids(records, post_ids),
        confidence=None,
        limitations=comparison.limitations,
    )


def _status_evidence(
    readiness: LearningReadiness,
    records: Sequence[PostPerformanceRecord],
) -> LearningEvidence:
    """一条"目前还不能学"的证据：如实说明原因，并带上依据。

    它不是失败信号，是**结论本身**——当前数据不足时，正确的输出就是这一条。
    工作单第三十一节：真实数据不足以产生任何比较结论时，必须明确输出
    `insufficient_sample`，而不是拉一条看似中性的空报告。
    """
    kind = _REASON_KINDS[readiness.reason]
    window = readiness.observation_window
    n = readiness.sample_count

    if kind is LearningEvidenceKind.INSUFFICIENT_SAMPLE:
        statement = (
            f"在 {window} 观察窗口下，有可用读数的已发布帖子只有 {n} 条，"
            f"低于比较下限 {MIN_SAMPLES_FOR_COMPARISON}。"
            "报告只报告样本量，不产出任何 cohort 比较。"
            "这个下限是采集量的门槛，不是统计显著性判断。"
        )
    elif kind is LearningEvidenceKind.WINDOW_MISMATCH:
        statement = (
            f"本次记录的观察跨越了不同窗口（要求的窗口是 {window}）。"
            "窗口不一致时不直接比较，报告不产出任何差异结论。"
        )
    elif kind is LearningEvidenceKind.MISSING_METRIC:
        statement = (
            f"在 {window} 观察窗口下，三类互动计数合计的可用读数少于比较下限 "
            f"{MIN_SAMPLES_FOR_COMPARISON}。缺失一律记为未知、不按 0 计入，"
            "报告不产出任何比较。"
        )
    else:  # INVALID_COMPARISON
        statement = (
            "样本量本身够了，但没有一组 cohort 满足比较成立的条件"
            "（同一维度、同一观察窗口、两组各自读数充足、样本互不重叠）。"
            "报告不产出任何差异结论。"
        )

    post_ids = tuple(sorted(r.published_post_id for r in records))
    snapshot_ids: list[str] = []
    for record in records:
        selected = select_window_observation(record.observations, window)
        if selected is not None:
            snapshot_ids.append(selected.snapshot_id)
    return LearningEvidence(
        id=f"learning:status:{_slug(window)}:{_slug(readiness.reason)}",
        type=kind,
        statement=statement,
        sample_count=readiness.sample_count,
        comparison_group=None,
        observation_window=window,
        metric=None,
        dimension=None,
        source_snapshot_ids=tuple(sorted(snapshot_ids)),
        source_post_ids=post_ids,
        source_x_post_ids=_x_post_ids(records, post_ids),
        confidence=None,
        limitations=readiness.limitations,
    )


def build_evidence(
    comparisons: Sequence[ObservedComparison],
    records: Sequence[PostPerformanceRecord],
    readiness: LearningReadiness,
) -> tuple[LearningEvidence, ...]:
    """比较 + 准入结果 → 证据。

    成立的比较各出一条 `COHORT_DIFFERENCE`；**不能学时恰好一条**状态证据
    （同 `app/feedback/signals.py::build_signals` 的"小样本恰好一条"写法）。
    顺序固定：先比较、后状态，截断/展示因此是确定性的。
    """
    evidence = [
        _comparison_evidence(c, records) for c in comparisons if c.valid
    ]
    if not readiness.learning_ready:
        evidence.append(_status_evidence(readiness, records))
    return tuple(evidence)


def evaluate_learning_readiness(
    records: Sequence[PostPerformanceRecord],
    cohorts: Sequence[CohortDefinition],
    comparisons: Sequence[ObservedComparison],
    *,
    window: str,
    min_samples: int = MIN_SAMPLES_FOR_COMPARISON,
) -> LearningReadiness:
    """确定性 learning gate（工作单第二十四节）。

    **顺序即优先级**：样本量 → 窗口一致 → 指标齐全 → 比较成立。
    先报最基础的那条，因为后面几条在样本不足时本来就没有意义。
    """
    usable_posts = [
        r for r in records if r.observation_window == window and not r.performance.is_empty
    ]
    sample_count = len(usable_posts)

    # 1. 样本量（分析单位是帖子）。
    sample_ok = sample_count >= min_samples

    # 2. 窗口一致：报告里出现的每一条记录都必须属于同一个窗口。
    #    这是对调用方的硬性要求——把两个窗口的记录混在一起比较，正是工作单
    #    第十六节禁止的"拿 30 分钟和 72 小时的点赞直接比"。
    off_window = sum(1 for r in records if r.observation_window != window)
    window_ok = off_window == 0

    # 3. 指标齐全：三类计数里至少有一类，在窗口内有 >= min_samples 条读数的可能。
    readings_by_metric = {
        metric: sum(
            1
            for r in usable_posts
            if getattr(r.performance, metric.value) is not None
        )
        for metric in _ALL_METRICS
    }
    total_readings = sum(readings_by_metric.values())
    metrics_ok = any(count >= min_samples for count in readings_by_metric.values())

    # 4. 比较成立：至少有一组 cohort 比较 valid。
    valid_comparisons = [c for c in comparisons if c.valid]
    comparison_ok = bool(valid_comparisons)

    checks = (
        ReadinessCheck(
            name="sample_count",
            passed=sample_ok,
            detail=(
                f"窗口 {window} 内有可用读数的帖子数 {sample_count}，"
                f"比较下限 {min_samples}。"
            ),
        ),
        ReadinessCheck(
            name="window_consistent",
            passed=window_ok,
            detail=(
                f"报告内所有记录都取自窗口 {window}。"
                if window_ok
                else f"报告内有 {off_window} 条记录取自 {window} 之外的窗口，"
                "窗口不一致时不直接比较。"
            ),
        ),
        ReadinessCheck(
            name="metrics_present",
            passed=metrics_ok,
            detail=(
                "窗口内三类计数的可用读数分别为："
                + "、".join(
                    f"{metric.value}={readings_by_metric[metric]}" for metric in _ALL_METRICS
                )
                + f"（合计 {total_readings}）。"
            ),
        ),
        ReadinessCheck(
            name="comparison_valid",
            passed=comparison_ok,
            detail=f"成立的可比较组：{len(valid_comparisons)} 组。",
        ),
    )

    if not sample_ok:
        reason = "insufficient_sample"
    elif not window_ok:
        reason = "inconsistent_window"
    elif not metrics_ok:
        reason = "missing_metrics"
    elif not comparison_ok:
        reason = "invalid_comparison"
    else:
        reason = "ready"

    limitations: list[str] = []
    if not sample_ok:
        limitations.append(
            f"有读数的帖子只有 {sample_count} 条，低于下限 {min_samples}；"
            "样本量不足时任何组间比较都不成立。"
        )
    if not window_ok:
        limitations.append(
            f"有 {off_window} 条记录取自窗口 {window} 之外；"
            "不同窗口的观察不放在一起比较，报告不产出任何差异结论。"
        )
    if not metrics_ok:
        limitations.append(
            "三类互动计数里有读数的总量不足，无法支撑任何指标上的比较。"
        )
    if not comparison_ok and sample_ok and metrics_ok:
        limitations.append(
            "样本量够了，但没有一组 cohort 同时满足比较成立的全部条件。"
        )

    return LearningReadiness(
        learning_ready=all(check.passed for check in checks),
        reason=reason,
        observation_window=window,
        sample_count=sample_count,
        post_count=len(records),
        checks=checks,
        automatic_modification=False,
        limitations=tuple(limitations),
    )


__all__ = [
    "build_evidence",
    "evaluate_learning_readiness",
    "evidence_id",
]
