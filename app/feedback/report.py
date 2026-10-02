"""把观察与信号组装成一份**经得起追问**的报告（Phase 5B.4）。

这是纯核心的出口。它做两件事，第二件比第一件重要：

1. 调 `build_signals` 产出信号；
2. **在返回之前把三条纪律检查一遍**，任何一条不满足就抛错：

   - 可追溯（工作单第二十五节）：每条信号的 `evidence` 非空，且每个 id
     都真的出现在这份报告的观察里。没有依据的结论不许出门。
   - 不声称因果（工作单第三、三十一节）：每条 `statement` 过因果词检查。
   - 诚实交代没算什么（工作单第三十五节）：`limitations` 必填，
     而且由**这份数据的具体缺口**推出来，不是一段模板文案。

把检查放在出口而不是散在生成处，是因为生成处将来会长出新的信号种类；
出口只有一个，漏检的可能性最小。
"""

from __future__ import annotations

from typing import Sequence

from app.feedback.metrics import (
    WINDOW_LABELS,
    Metrics,
    engagement_rate,
    window_bucket,
)
from app.feedback.schema import (
    FeedbackContext,
    FeedbackReport,
    FeedbackSignal,
    SnapshotObservation,
)
from app.feedback.signals import (
    MIN_SAMPLES_FOR_COMPARISON,
    assert_no_causal_claim,
    build_signals,
)

#: 进 checkpoint 的上下文最多带几条信号。
#:
#: 不是为了省空间，是为了让 Writer 看到的是一段**能读完**的摘要——
#: 一屏塞不下的历史会诱导模型去抓它抓得住的那一条（通常是最大的那个数），
#: 而那正是工作单第四十二节要避免的"把某一条观察当成目标"。
MAX_CONTEXT_SIGNALS = 6

#: 每一份报告都带着的底线说明。它们是**口径**，不随数据变化。
BASE_LIMITATIONS: tuple[str, ...] = (
    "本报告只描述观察到的分布，不做任何因果结论；同时出现不等于谁导致了谁。",
    "点赞、回复、转发、引用是四类不同的行为，本报告不把它们合成单一分数。",
    "指标为公开计数；曝光与收藏从未采集，恒为未知，不按 0 计入。",
    "没有对照组、没有实验设计；任何组间差异都可能来自内容之外的变量。",
)


def _observation_ids(observations: Sequence[SnapshotObservation]) -> set[str]:
    return {o.snapshot_id for o in observations}


def _validate_signals(
    signals: Sequence[FeedbackSignal], observations: Sequence[SnapshotObservation]
) -> None:
    """可追溯性 + 无因果措辞。见模块开头。"""
    known = _observation_ids(observations)
    for signal in signals:
        if not signal.evidence:
            raise ValueError(
                f"信号 {signal.kind.value} 没有依据（evidence 为空）：{signal.statement!r}"
            )
        unknown = [sid for sid in signal.evidence if sid not in known]
        if unknown:
            raise ValueError(
                f"信号 {signal.kind.value} 引用了本报告里不存在的观察 {unknown!r}"
            )
        assert_no_causal_claim(signal.statement, where=f"signal {signal.kind.value}")


def _data_limitations(
    observations: Sequence[SnapshotObservation],
    signals: Sequence[FeedbackSignal],
    min_samples: int,
) -> list[str]:
    """由这份数据的具体缺口推出"哪里不足"。"""
    notes: list[str] = []
    n = len(observations)

    if n == 0:
        notes.append("当前没有可用的观察快照，因此没有产出任何信号。")
    elif n < min_samples:
        notes.append(
            f"观察数 n={n}，低于比较下限 {min_samples}，"
            "本报告只报告样本量，不产出任何比较结论。"
        )

    missing_metrics = sorted(
        {s.metric for s in signals if s.kind.value == "missing_metric" and s.metric}
    )
    if missing_metrics:
        notes.append(
            "部分观察缺少以下计数，相关统计只在有读数的子集上进行："
            + "、".join(missing_metrics)
            + "。"
        )

    if observations and all(o.impressions is None for o in observations):
        notes.append("曝光量为未知，因此互动率无法计算（不是零互动率）。")

    # 观察落在哪些时间档：让读者知道手上看到的是发布后多久的快照。
    buckets = {b for b in (window_bucket(o.hours_since_publish) for o in observations) if b}
    if buckets:
        ordered = [label for label in WINDOW_LABELS if label in buckets]
        notes.append("本次观察覆盖的时间档：" + "、".join(ordered) + "。")
    return notes


def build_report(
    observations: Sequence[SnapshotObservation],
    *,
    min_samples: int = MIN_SAMPLES_FOR_COMPARISON,
) -> FeedbackReport:
    """观察 → 报告。所有校验在返回前完成。"""
    signals = build_signals(observations, min_samples=min_samples)
    _validate_signals(signals, observations)
    limitations = (*BASE_LIMITATIONS, *_data_limitations(observations, signals, min_samples))
    return FeedbackReport(
        sample_count=len(observations),
        observations=tuple(observations),
        signals=signals,
        limitations=limitations,
    )


def build_feedback_context(
    report: FeedbackReport,
    *,
    limit: int = MAX_CONTEXT_SIGNALS,
) -> FeedbackContext:
    """报告 → 进 checkpoint 的 Writer 上下文。

    只留信号与去重后的观察 id，**不搬观察本身**：Writer 要的是"我过去发的东西
    后来大致怎么样"，不需要逐条明细（工作单第四十二节）。截断按信号在报告里的
    顺序进行——顺序由 `build_signals` 固定，所以截断是确定性的，
    不会因为字典序之类的东西每次换一批。
    """
    signals = tuple(report.signals[:limit])
    return FeedbackContext(
        sample_count=report.sample_count,
        post_count=len({o.published_post_id for o in report.observations}),
        snapshot_count=len(report.observations),
        latest_observed_at=max(
            (o.observed_at for o in report.observations if o.observed_at is not None),
            default=None,
        ),
        signals=signals,
        limitations=tuple(report.limitations),
        source_snapshot_ids=tuple(o.snapshot_id for o in report.observations),
    )


__all__ = [
    "BASE_LIMITATIONS",
    "MAX_CONTEXT_SIGNALS",
    "build_feedback_context",
    "build_report",
]
