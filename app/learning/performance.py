"""表现向量的确定性算术：窗口取值、分布、离群点（Phase 5C）。

这一整个模块是**纯的**：没有 IO、没有随机、没有 `datetime.now()`、不读配置
（同 `app/feedback/metrics.py` 的理由）。它算出来的东西**不落库**——逐位可复算，
存一份只会多一个将来会漂移的真相来源。

## 三条纪律，每条都对应一种具体的误读

1. **分析单位是帖子，不是快照。** 一条帖子的 1h / 6h / 24h 三条快照是**同一条
   帖子的重复观察**。`vector_at_window` 因此在一个窗口里**只取一条**（该窗口内
   最新的一条），而不是把窗口内的观察平均或相加。把三条当三个样本，就是在
   小样本上造"显著差异"最快的一条路。

2. **未知是 `None`，不是 0。** 一个窗口里没有观察 → 三个分量全是 `None`；
   某个计数没读回来 → 那个分量是 `None`。**不补 0**：补 0 会把它压到分布下沿，
   然后变成一条"这一类表现更差"的假结论。

3. **离群点只标记，不删除。** 真实爆款本身就是数据（工作单第十五节）。
   `mark_outliers` 返回的是一串布尔，调用方据此计数；没有任何一条代码路径会
   把标记为离群的点从分布里剔除——`distribution` 永远在**全部**读数上计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from app.feedback.metrics import (
    WINDOW_LABELS,
    Metrics,
    amplification,
    attention,
    conversation,
    window_bucket,
)
from app.feedback.schema import SnapshotObservation
from app.learning.schema import PerformanceMetric, PerformanceVector

#: 没有 `observed_at` 的观察在排序时的哨兵。它只用于让排序有全序，
#: **不**参与任何指标计算，也**不**顶替一个缺失的时刻显示给人看。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: 正文长度的分档边界（字符数）。这是我们**自己**的取值，不是 X 的规则，
#: 也不是语料的结论——写在这里是为了让"长度桶"可复现、可解释。
#: 边界选在 persona 常见上限（140）附近，把"明显偏短 / 典型 / 明显偏长"分开。
LENGTH_BUCKET_SHORT_MAX = 80
LENGTH_BUCKET_MEDIUM_MAX = 140

#: 长度桶的封闭标签，供展示层复用（不要在各处再写一遍字符串）。
LENGTH_BUCKET_LABELS: dict[str, str] = {
    "short": "偏短（<= 80 字）",
    "medium": "典型（81-140 字）",
    "long": "偏长（> 140 字）",
}

#: 少于这么多读数就不判离群：IQR 栅栏在 n<4 时几乎完全由端点决定，
#: 标出来的"离群"多半只是样本太小。**不判不等于删**——这些点照样算进分布。
OUTLIER_MIN_SAMPLE = 4


def length_bucket(content_length: int | None) -> str | None:
    """正文长度 → 桶标签。未知 → `None`（**不是** 0，也不是某个默认桶）。"""
    if content_length is None:
        return None
    if content_length <= LENGTH_BUCKET_SHORT_MAX:
        return "short"
    if content_length <= LENGTH_BUCKET_MEDIUM_MAX:
        return "medium"
    return "long"


def performance_vector(observation: SnapshotObservation | object) -> PerformanceVector:
    """一条观察 → 三个分量。复用第四层的定义，**不另立一套**。

    `app/feedback/metrics.py` 已经定义了 attention / conversation / amplification，
    这里只是把它们包成一个对象；再实现一遍必然会在某次改动后与第四层不一致，
    而那种不一致不会有任何报错。
    """
    metrics = Metrics.from_observation(observation)
    return PerformanceVector(
        attention=attention(metrics),
        conversation=conversation(metrics),
        amplification=amplification(metrics),
    )


def _observation_order_key(observation: SnapshotObservation) -> tuple:
    """观察的排序键：有 `observed_at` 的排在无时刻的之后，再按时刻、再按 id。

    第一段用布尔值是为了避免把 `None` 与 `datetime` 直接比较——那会抛
    `TypeError`，而这里只是要一个确定的全序，不是要做时间运算。
    """
    return (
        observation.observed_at is not None,
        observation.observed_at or _EPOCH,
        observation.snapshot_id,
    )


def select_window_observation(
    observations: Sequence[SnapshotObservation], window: str
) -> SnapshotObservation | None:
    """在**指定窗口**内取最新的一条观察；没有则 `None`。

    取"最新"而不是"第一条"：计数是单调累计的，窗口内越晚的读数越接近这个窗口
    的完整画面。取第一条会把每个窗口都读成"刚进窗口那一刻"的数字。
    """
    inside = [o for o in observations if window_bucket(o.hours_since_publish) == window]
    if not inside:
        return None
    return max(inside, key=_observation_order_key)


def vector_at_window(
    observations: Sequence[SnapshotObservation], window: str
) -> PerformanceVector:
    """一条帖子在指定窗口的表现向量。窗口内没有观察 → 三个分量全 `None`。"""
    observation = select_window_observation(observations, window)
    if observation is None:
        return PerformanceVector()
    return performance_vector(observation)


def dominant_window(
    observation_sequences: Sequence[Sequence[SnapshotObservation]],
    *,
    labels: Sequence[str] = WINDOW_LABELS,
) -> str:
    """在给定的一批帖子里，**读数最多**的观察窗口。

    用作默认窗口：真实数据只有 `0-1h` 的观察时，默认就该是 `0-1h`，
    否则报告会因为选了一个没人有读数的窗口而只说"没有数据"。
    并列时按 `labels` 的固定顺序取靠前的那个——**确定性**，不随遍历顺序漂。
    """
    counts = {label: 0 for label in labels}
    for observations in observation_sequences:
        seen = {window_bucket(o.hours_since_publish) for o in observations}
        for label in seen:
            if label in counts:
                counts[label] += 1
    best = max(counts.values()) if counts else 0
    if best == 0:
        return labels[0]
    for label in labels:
        if counts[label] == best:
            return label
    return labels[0]  # pragma: no cover - 上面必然命中


# --- 分布 -------------------------------------------------------------------


@dataclass(frozen=True)
class Distribution:
    """一组读数的分布。空序列 → 除 `sample_count` 外全部 `None`。

    `median` / `p25` / `p75` 是**主口径**；`mean` 只是**辅助**（工作单第十四节）。
    保留 `mean` 不是为了用它下结论，而是为了让"为什么不用均值"这件事在报告里
    可核对——两个口径都摆出来，读者自己就能看到异常值把均值拉走了多少。
    """

    sample_count: int
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    mean: float | None = None
    minimum: float | None = None
    maximum: float | None = None


def percentile(values: Sequence[float], q: float) -> float | None:
    """线性插值分位数（`q ∈ [0, 1]`）。空序列 → `None`。

    自己实现而不是用 `statistics.quantiles`：标准库对 n<2 直接抛错，而"只有一条
    读数"在本层是常见且合法的状态（一个 cohort 里只有一条帖子有该指标读数）。
    单点序列返回它自己，这是"分位数在这个样本上就是它"的诚实答案。
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def distribution(values: Sequence[float]) -> Distribution:
    """一组读数 → 分布。**空序列不是 (0,0,0)**，是全部 `None`。"""
    if not values:
        return Distribution(sample_count=0)
    ordered = sorted(float(v) for v in values)
    return Distribution(
        sample_count=len(ordered),
        median=percentile(ordered, 0.5),
        p25=percentile(ordered, 0.25),
        p75=percentile(ordered, 0.75),
        mean=sum(ordered) / len(ordered),
        minimum=ordered[0],
        maximum=ordered[-1],
    )


def mark_outliers(values: Sequence[float]) -> tuple[bool, ...]:
    """IQR 栅栏：落在 `[p25 - 1.5·IQR, p75 + 1.5·IQR]` 之外的标记为离群。

    **只标记，不删除。** 返回的布尔串与 `values` 一一对应，调用方据此计数。

    读数少于 `OUTLIER_MIN_SAMPLE` 时一律不标（全是 `False`）：样本太小时栅栏
    几乎完全由端点决定，标出来的"离群"多半只是噪声。注意 `IQR = 0` 时栅栏
    收缩成一个点，此时**任何**偏离该点的值都会被标记——这正是我们想要的
    （一水儿的 0 里出现一个 50，就是异常）。
    """
    if len(values) < OUTLIER_MIN_SAMPLE:
        return tuple(False for _ in values)
    p25 = percentile(values, 0.25)
    p75 = percentile(values, 0.75)
    assert p25 is not None and p75 is not None  # len >= 1
    iqr = p75 - p25
    low = p25 - 1.5 * iqr
    high = p75 + 1.5 * iqr
    return tuple(v < low or v > high for v in values)


def metric_value(vector: PerformanceVector, metric: PerformanceMetric) -> int | None:
    """按指标取分量。**未知返回 `None`**（对 `PostPerformanceRecord` 的薄封装）。

    单独写成函数而不是让调用方 `getattr`：可空语义必须只有一处实现。
    """
    return vector.value_of(metric)


__all__ = [
    "LENGTH_BUCKET_LABELS",
    "LENGTH_BUCKET_MEDIUM_MAX",
    "LENGTH_BUCKET_SHORT_MAX",
    "OUTLIER_MIN_SAMPLE",
    "Distribution",
    "distribution",
    "dominant_window",
    "length_bucket",
    "mark_outliers",
    "metric_value",
    "percentile",
    "performance_vector",
    "select_window_observation",
    "vector_at_window",
]
