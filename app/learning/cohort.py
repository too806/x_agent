"""Cohort：把帖子按**确定性维度**分组，并在同一窗口、同一指标上比较（Phase 5C）。

## 为什么必须"同窗口、同维度、互不重叠"

工作单第十一节讲的是伪因果最经典的形态：

    question hook（一堆全是 AI Agent 新闻）
    vs
    statement hook（一堆全是芯片行情）
    → "hook 导致表现不同"

分组变量与选题同时变了，差异到底来自哪一个，从这批数据里**分不出来**。本模块
能做的三件事，恰好对应工作单第十一节要求记录的三样：

    sample_count    —— 每组多少个帖子
    time_window     —— 看的是发布后多久
    selection_rule  —— 怎么选的组

再加一件本仓库**做不到**的：控制 topic。没有确定性的选题分类器，所以两组 topic
组成不同时不会被悄悄忽略，而是写进 `limitations`——差异可能来自选题，这句话必须
留在报告里。

## 比较只在**成立**时产出结论

`valid=False` 的比较不是"较弱的结论"，是**没有结论**：不成立就不许出现
"更高/更低"。这一条由 `ObservedComparison.valid` 与固定的陈述模板共同保证——
模板里没有"更好"，只有"观察到更高"。

## 分析单位仍然是帖子

一个 cohort 的 `sample_count` 是**帖子数**。同一帖子的多条快照在窗口取值时已经
收敛成一条（见 `performance.py`），所以这里不会有重复计数。
"""

from __future__ import annotations

from typing import Sequence

from app.feedback.signals import MIN_SAMPLES_FOR_COMPARISON
from app.learning.performance import (
    Distribution,
    distribution,
    mark_outliers,
    metric_value,
    select_window_observation,
)
from app.learning.schema import (
    CohortDefinition,
    CohortDimension,
    CohortStats,
    ObservedComparison,
    PerformanceMetric,
    PostPerformanceRecord,
)

#: 这些取值表示"没有可用分类"，**不参与比较**。StyleRecipe 的默认枚举值是
#: `unclassified`；把一整组 unclassified 拿去和 question 比，比的是"有 recipe"
#: 与"没有 recipe"，不是 hook 的差别。
UNCLASSIFIED_VALUES: frozenset[str] = frozenset({"unclassified", "unknown", ""})

#: 布尔维度在 cohort 取值里的编码。**不用** "True"/"False" 字符串：
#: 那会与一个恰好叫这个名字的枚举值混起来。
BOOL_TRUE = "yes"
BOOL_FALSE = "no"

#: 维度 → 人读名。封闭表；展示层复用，不要在各处再写一遍。
DIMENSION_LABELS: dict[str, str] = {
    CohortDimension.STRUCTURE_TYPE.value: "写作结构",
    CohortDimension.HOOK_TYPE.value: "开头方式",
    CohortDimension.STANCE.value: "判断强度",
    CohortDimension.LENGTH_BUCKET.value: "正文长度档",
    CohortDimension.COUNTERPOINT_AWARENESS.value: "是否带反方视角",
    CohortDimension.SCOPE_EXCEEDS_EVIDENCE.value: "结论是否超出证据",
}

#: 指标 → 人读名。
METRIC_LABELS: dict[str, str] = {
    PerformanceMetric.ATTENTION.value: "点赞（注意力）",
    PerformanceMetric.CONVERSATION.value: "对话（回复+引用）",
    PerformanceMetric.AMPLIFICATION.value: "转发（扩散）",
}

#: 布尔维度的取值 → 人读名。
_BOOL_LABELS: dict[str, str] = {BOOL_TRUE: "有", BOOL_FALSE: "无"}


def dimension_label(dimension: CohortDimension | str) -> str:
    key = getattr(dimension, "value", dimension)
    return DIMENSION_LABELS.get(key, str(key))


def metric_label(metric: PerformanceMetric | str) -> str:
    key = getattr(metric, "value", metric)
    return METRIC_LABELS.get(key, str(key))


def _value_label(dimension: CohortDimension, value: str) -> str:
    if value in _BOOL_LABELS:
        return f"{value}（{_BOOL_LABELS[value]}）"
    return value


def dimension_value(
    record: PostPerformanceRecord, dimension: CohortDimension
) -> str | None:
    """一条记录在某个维度上的取值。**取不到就是 `None`**，不是某个默认桶。"""
    if dimension is CohortDimension.STRUCTURE_TYPE:
        return record.structure_type
    if dimension is CohortDimension.HOOK_TYPE:
        return record.hook_type
    if dimension is CohortDimension.STANCE:
        return record.stance
    if dimension is CohortDimension.LENGTH_BUCKET:
        return record.length_bucket
    if dimension is CohortDimension.COUNTERPOINT_AWARENESS:
        if record.counterpoint_awareness is None:
            return None
        return BOOL_TRUE if record.counterpoint_awareness else BOOL_FALSE
    if dimension is CohortDimension.SCOPE_EXCEEDS_EVIDENCE:
        if record.scope_exceeds_evidence is None:
            return None
        return BOOL_TRUE if record.scope_exceeds_evidence else BOOL_FALSE
    return None  # pragma: no cover - 封闭枚举，上面已穷尽


def _selected_snapshot_id(record: PostPerformanceRecord) -> str | None:
    observation = select_window_observation(record.observations, record.observation_window)
    return observation.snapshot_id if observation is not None else None


def build_cohorts(
    records: Sequence[PostPerformanceRecord],
    dimension: CohortDimension,
    *,
    window: str,
) -> tuple[CohortDefinition, ...]:
    """按维度分组。取值为 `None` 的帖子**不进任何组**（不塞一个"未知"组去凑数）。

    分组顺序是确定的（按取值字符串排序），于是同一批输入每次得到同一份 cohort 列表。

    **窗口不等于本次窗口的记录一律不进组**：一个 cohort 的 `observation_window`
    是对它全部成员的事实陈述，把 `1-6h` 的记录塞进一个自称 `24-72h` 的组里，
    会让这份陈述变成假的。混了窗口的记录不会被静默丢弃——闸门的
    `window_consistent` 会先拒绝整份报告，`limitations` 里也会说明有几条被排除。
    """
    groups: dict[str, list[PostPerformanceRecord]] = {}
    for record in records:
        if record.observation_window != window:
            continue
        value = dimension_value(record, dimension)
        if value is None:
            continue
        groups.setdefault(value, []).append(record)

    cohorts: list[CohortDefinition] = []
    for value in sorted(groups):
        members = groups[value]
        snapshot_ids = tuple(
            sid for sid in (_selected_snapshot_id(r) for r in members) if sid is not None
        )
        topics = tuple(sorted({r.topic for r in members if r.topic}))
        limitations: list[str] = []
        if value in UNCLASSIFIED_VALUES:
            limitations.append(
                f"取值「{value}」表示没有可用的分类（例如没有 StyleRecipe），"
                "不参与任何比较。"
            )
        if len({r.published_post_id for r in members}) < len(members):  # pragma: no cover
            limitations.append("同一帖子在本组里出现了多次，已按帖子去重统计。")
        cohorts.append(
            CohortDefinition(
                dimension=dimension,
                value=value,
                observation_window=window,
                selection_rule=(
                    f"按 {dimension_label(dimension)} == {_value_label(dimension, value)} 选取；"
                    f"观察窗口固定为 {window}；分析单位为**帖子**（每条帖在该窗口只贡献一个值）。"
                ),
                sample_count=len({r.published_post_id for r in members}),
                post_ids=tuple(sorted(r.published_post_id for r in members)),
                snapshot_ids=snapshot_ids,
                topics=topics,
                limitations=tuple(limitations),
            )
        )
    return tuple(cohorts)


def cohort_stats(
    records: Sequence[PostPerformanceRecord],
    cohort: CohortDefinition,
    metric: PerformanceMetric,
) -> CohortStats:
    """一个 cohort 在某指标上的分布。**只统计有读数的帖子，不补 0。**"""
    member_ids = set(cohort.post_ids)
    members = [r for r in records if r.published_post_id in member_ids]
    pairs = [
        (r, value)
        for r in members
        if (value := metric_value(r.performance, metric)) is not None
    ]
    values = [value for _, value in pairs]
    dist: Distribution = distribution(values)
    flags = mark_outliers(values)
    outlier_pairs = [pair for pair, flag in zip(pairs, flags) if flag]
    return CohortStats(
        value=cohort.value,
        sample_count=dist.sample_count,
        median=dist.median,
        p25=dist.p25,
        p75=dist.p75,
        mean=dist.mean,
        minimum=dist.minimum,
        maximum=dist.maximum,
        outlier_count=len(outlier_pairs),
        post_ids=tuple(sorted(r.published_post_id for r, _ in pairs)),
        snapshot_ids=tuple(
            sid for sid in (_selected_snapshot_id(r) for r, _ in pairs) if sid is not None
        ),
    )


def _fmt(value: float | None) -> str:
    if value is None:
        return "未知"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _topic_note(
    a: CohortDefinition, b: CohortDefinition, dimension: CohortDimension
) -> list[str]:
    """两组 topic 组成不同时的一句限制。

    本仓库没有确定性的选题分类器，因此**无法**把选题作为协变量控制掉。
    能做的是把"两组选题不一样"这件事说出来——否则读者会把它读成
    "这个维度导致了这个差异"（工作单第十一节）。
    """
    if not a.topics or not b.topics:
        return []
    if set(a.topics).isdisjoint(b.topics):
        return [
            f"两组的选题完全不同（{dimension_label(dimension)} 之外还有一个变量在变），"
            "差异可能来自选题，本报告没有能力把选题控制掉。"
        ]
    return []


def compare_cohorts(
    records: Sequence[PostPerformanceRecord],
    cohorts: Sequence[CohortDefinition],
    *,
    dimension: CohortDimension,
    window: str,
    metric: PerformanceMetric,
    min_samples: int = MIN_SAMPLES_FOR_COMPARISON,
) -> tuple[ObservedComparison, ...]:
    """同一维度的两两比较。**每一对都产出对象，但只有成立的那些带结论。**

    不成立也返回对象（`valid=False` + 原因）：一个被静默丢弃的比较，读的人
    无从知道"这里本来想比什么、为什么没比"。工作单第三十五节要求"哪里不足"
    必须摆出来，这条同样适用。
    """
    relevant = [c for c in cohorts if c.dimension is dimension]
    # 去重（values 已排序，cohorts 由 build_cohorts 保证唯一，这里只是防御）。
    relevant = sorted({c.value: c for c in relevant}.values(), key=lambda c: c.value)

    comparisons: list[ObservedComparison] = []
    for i, a in enumerate(relevant):
        for b in relevant[i + 1 :]:
            stats_a = cohort_stats(records, a, metric)
            stats_b = cohort_stats(records, b, metric)
            limitations = _topic_note(a, b, dimension)

            reason: str | None = None
            if a.value in UNCLASSIFIED_VALUES or b.value in UNCLASSIFIED_VALUES:
                reason = "其中一组的取值表示没有可用的分类，两组不构成同一比较基础。"
            elif a.value == b.value:  # pragma: no cover - 键唯一，防御
                reason = "两组取值相同，不构成比较。"
            elif set(a.post_ids) & set(b.post_ids):
                reason = "两组包含同一条帖子，样本不独立。"
            elif a.observation_window != window or b.observation_window != window:
                reason = "两组的观察窗口不同，不能直接比较（发布后 1 小时与 72 小时不是同一件事）。"
            elif a.sample_count < min_samples or b.sample_count < min_samples:
                reason = (
                    f"其中一组的帖子数低于比较下限 {min_samples}"
                    f"（{a.value}: n={a.sample_count}，{b.value}: n={b.sample_count}），"
                    "样本量不足时不产出差异结论。"
                )
            elif stats_a.sample_count < min_samples or stats_b.sample_count < min_samples:
                reason = (
                    f"其中一组在「{metric_label(metric)}」上有读数的帖子数低于下限 "
                    f"{min_samples}（{a.value}: {stats_a.sample_count}，"
                    f"{b.value}: {stats_b.sample_count}），读数不足，不补值、不比较。"
                )
            elif stats_a.median is None or stats_b.median is None:
                reason = "其中一组没有可用的中位数。"

            valid = reason is None
            difference = (
                stats_b.median - stats_a.median
                if valid and stats_a.median is not None and stats_b.median is not None
                else None
            )

            if valid:
                # 方向只在**确实不等**时给出。中位数相等时说某一方"更高"是在编一个
                # 不存在的差异——那正是本层要防的那类错误，只不过它发生在措辞上。
                if difference is None or difference == 0:
                    direction = "在当前样本中两组的中位数相同，没有观察到方向上的差异。"
                else:
                    higher = b.value if difference > 0 else a.value
                    direction = (
                        f"在当前样本中观察到「{_value_label(dimension, higher)}」更高。"
                    )
                statement = (
                    f"在 {window} 观察窗口下，按{dimension_label(dimension)}分组，"
                    f"{_value_label(dimension, a.value)}（n={stats_a.sample_count}）的"
                    f"{metric_label(metric)}中位数为 {_fmt(stats_a.median)}（p25 "
                    f"{_fmt(stats_a.p25)}，p75 {_fmt(stats_a.p75)}），"
                    f"{_value_label(dimension, b.value)}（n={stats_b.sample_count}）为 "
                    f"{_fmt(stats_b.median)}（p25 {_fmt(stats_b.p25)}，"
                    f"p75 {_fmt(stats_b.p75)}）；{direction}"
                    "这是观察到的差异，不构成任何因果结论。"
                )
            else:
                statement = (
                    f"在 {window} 观察窗口下，按{dimension_label(dimension)}分组，"
                    f"{_value_label(dimension, a.value)}（n={a.sample_count}）与 "
                    f"{_value_label(dimension, b.value)}（n={b.sample_count}）的"
                    f"{metric_label(metric)}不构成可比：{reason}"
                    "据此本报告不产出任何差异结论。"
                )

            source_ids = tuple(
                sorted(set(stats_a.snapshot_ids) | set(stats_b.snapshot_ids))
            )
            comparisons.append(
                ObservedComparison(
                    dimension=dimension,
                    observation_window=window,
                    metric=metric,
                    group_a=stats_a,
                    group_b=stats_b,
                    observed_difference=difference,
                    valid=valid,
                    invalid_reason=reason,
                    statement=statement,
                    source_snapshot_ids=source_ids,
                    limitations=tuple(limitations),
                )
            )
    return tuple(comparisons)


__all__ = [
    "BOOL_FALSE",
    "BOOL_TRUE",
    "DIMENSION_LABELS",
    "METRIC_LABELS",
    "UNCLASSIFIED_VALUES",
    "build_cohorts",
    "cohort_stats",
    "compare_cohorts",
    "dimension_label",
    "dimension_value",
    "metric_label",
]
