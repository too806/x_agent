"""学习准备度离线基准的**手写**数据（Phase 5C 工作单第二十七节）。

九组记录，全部合成：不碰 X、不碰 LLM、不读数据库、不取挂钟时间。每组只差
**一个变量**，于是"闸门为什么在这个位置拒绝"是可以指认的，而不是一句大概。

## 九组各自在证明什么

| category | 数据 | 必须得到的判定 |
| --- | --- | --- |
| `one_post` | 1 条帖子，2 条快照（都在 0-1h） | `insufficient_sample`：**帖子数**是 1，不是 2 |
| `small_sample` | 4 条帖子 | `insufficient_sample`，且不产出任何差异结论 |
| `balanced_cohorts` | 5 question + 5 statement，三指标齐 | `ready`，比较成立 |
| `unequal_cohorts` | 5 + 3 | 样本量够，但分组不成立 → 不产出差异结论 |
| `missing_metrics` | 6 条帖子，每条只读回一类计数 | `missing_metrics`：**不补 0**，也不比较 |
| `different_windows` | 5 条在 `1-6h`、5 条在 `24-72h` | `inconsistent_window`：拒绝跨窗口直接比较 |
| `outlier` | 5 + 5，其中一条 900 | 离群点被**标记**、不被删除；中位数不动 |
| `causal_language` | 5 + 5，差异干净 | 结论是观察措辞，一个因果词都没有 |
| `invalid_comparison` | 5 + 5，其中一组三指标里有读数的都不到 5 | `invalid_comparison`：读数不足时不比较 |

## 预期是数据，不是注释

每组带一份 `expect`：`(字段路径, 关系, 期望值)`。路径由 `selftest.py::_resolve`
求值，关系词是封闭表（多一个词就要在那边加一个分支，所以它不封闭的时候会报错，
而不是被猜到语义）。写成数据而不是散在检查函数里，是为了让"这一组应当产出什么"
在报告里**可读**，并且失败信息指向具体那一条。

## 为什么记录里的 `performance` 用真函数算

`make_record` 不手写 `PerformanceVector`，而是把观察序列交给
`performance.vector_at_window(observations, window)`——真机装配走的是同一个函数。
手写一个"我以为它算出来是这样"的向量，测的就是我的假设，而不是代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agent.schemas import ViewpointStance
from app.feedback.schema import SnapshotObservation
from app.feedback.signals import MIN_SAMPLES_FOR_COMPARISON
from app.learning.performance import length_bucket, vector_at_window
from app.learning.schema import PostPerformanceRecord
from app.style.taxonomy import HookType, StructureType

#: 所有记录共用的发布时刻。**与观察时刻分开**：两者的差是 `hours_since_publish`，
#: 由写入方算好，这里只把结果填进去（同 `evals/feedback/cases.py` 的纪律）。
PUBLISHED_AT = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)

#: 默认观察窗口。48 小时落在 `24-72h` 档。
WINDOW = "24-72h"
#: `1-6h` 档里的一个时刻。
EARLY_WINDOW = "1-6h"
EARLY_HOURS = 3.0
#: `24-72h` 档里的一个时刻。
HOURS = 48.0

#: 一条观察的紧凑写法：`(小时, likes, replies, reposts, quotes)`。
Observation = tuple[float, int | None, int | None, int | None, int | None]


@dataclass(frozen=True)
class Case:
    """一组记录，以及它必须得到的判定。"""

    category: str
    note: str
    #: 交给 `build_learning_report` 的窗口。
    window: str
    records: tuple[PostPerformanceRecord, ...]
    #: `(字段路径, 关系, 期望值)`。
    expect: list[tuple[str, str, Any]] = field(default_factory=list)


def spec(
    likes: int | None = None,
    conversation: int | None = None,
    amplification: int | None = None,
    *,
    hours: float = HOURS,
) -> Observation:
    """把三类计数写成一条观察。

    `conversation` 是 `replies + quotes` 的和，这里落在 `replies` 上、`quotes` 记 0
    （0 是一个**读到的**值，不是缺失）。某一类不需要时传 `None`——它的两个来源
    都写 `None`，"全有才求和"于是给出 `None`，而这正是我们要的"读不到"。
    """
    return (
        hours,
        likes,
        conversation,
        amplification,
        0 if conversation is not None else None,
    )


def _observation(
    post_index: int, seq: int, item: Observation
) -> SnapshotObservation:
    hours, likes, replies, reposts, quotes = item
    return SnapshotObservation(
        snapshot_id=f"snap-{post_index}-{seq}",
        x_post_id=f"x-{post_index}",
        published_post_id=f"post-{post_index}",
        observed_at=PUBLISHED_AT + timedelta(hours=hours),
        hours_since_publish=hours,
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
    )


def make_record(
    post_index: int,
    *,
    window: str = WINDOW,
    observations: tuple[Observation, ...],
    hook: str = HookType.QUESTION.value,
    structure: str = StructureType.OPINION_FIRST.value,
    stance: str = ViewpointStance.MODERATE.value,
    counterpoint: bool | None = False,
    scope_exceeds: bool | None = False,
    content_length: int = 120,
    topic: str = "主题甲",
) -> PostPerformanceRecord:
    """一条合成记录。维度默认值**在所有组里都一样**——差异只由调用方显式改。

    默认全部相同是有意的：一组数据里若有两个维度同时不同，"差异来自哪一个"
    就分不出来了，而这正是本层要防的那类比较（工作单第十一节）。
    """
    obs = tuple(
        _observation(post_index, seq, item) for seq, item in enumerate(observations)
    )
    return PostPerformanceRecord(
        published_post_id=f"post-{post_index}",
        x_post_id=f"x-{post_index}",
        published_at=PUBLISHED_AT,
        run_id=f"run-{post_index}",
        topic=topic,
        trend_score=10.0,
        structure_type=structure,
        hook_type=hook,
        effective_target_length=140,
        stance=stance,
        counterpoint_awareness=counterpoint,
        scope_exceeds_evidence=scope_exceeds,
        critic_score=7.0,
        content_length=content_length,
        length_bucket=length_bucket(content_length),
        observation_window=window,
        performance=vector_at_window(obs, window),
        observations=obs,
        limitations=(),
    )


def _group(
    indices: range,
    *,
    hook: str,
    observations_for,
    window: str = WINDOW,
    **kwargs: Any,
) -> list[PostPerformanceRecord]:
    return [
        make_record(i, window=window, observations=observations_for(i), hook=hook, **kwargs)
        for i in indices
    ]


# ---------------------------------------------------------------------------
# 九组数据
# ---------------------------------------------------------------------------


def _one_post() -> Case:
    """1 条帖子 + 2 条快照。分析单位是**帖子**，所以 n = 1。"""
    record = make_record(
        0,
        window="0-1h",
        observations=(spec(5, 1, 0, hours=0.3), spec(7, 2, 1, hours=0.9)),
    )
    return Case(
        category="one_post",
        note="一条帖子两条快照：帖子数是 1，快照数是 2，够不上任何比较",
        window="0-1h",
        records=(record,),
        expect=[
            # 两条快照在同一条帖子、同一个窗口里**只贡献一个值**（窗口内最新那条）。
            ("sample_count", "==", 1),
            ("snapshot_count", "==", 2),
            ("post_count", "==", 1),
            ("ready", "is", False),
            ("reason", "==", "insufficient_sample"),
            ("comparison_count", "==", 0),
            ("evidence_kinds", "contains", "insufficient_sample"),
            ("evidence_count", "==", 1),
        ],
    )


def _small_sample() -> Case:
    """4 条帖子，分成两组：样本量不够，两组都产不出结论。"""
    records = tuple(
        _group(
            range(0, 2),
            hook=HookType.QUESTION.value,
            observations_for=lambda i: (spec(20, 5, 2),),
        )
        + _group(
            range(2, 4),
            hook=HookType.STATEMENT.value,
            observations_for=lambda i: (spec(4, 1, 0),),
        )
    )
    return Case(
        category="small_sample",
        note="4 条帖子低于下限 5：只报样本量，不产出任何差异结论",
        window=WINDOW,
        records=records,
        expect=[
            ("sample_count", "==", 4),
            ("sample_count", "<", MIN_SAMPLES_FOR_COMPARISON),
            ("ready", "is", False),
            ("reason", "==", "insufficient_sample"),
            ("valid_comparison_count", "==", 0),
            # 不成立也**摆出来**：读的人要知道"本来想比什么、为什么没比"。
            ("comparison_count", ">", 0),
            ("evidence_kinds", "contains", "insufficient_sample"),
            ("evidence_count", "==", 1),
        ],
    )


def _balanced_cohorts() -> Case:
    """5 + 5，三指标齐全：闸门这时才允许说"可以作为学习材料"。"""
    records = tuple(
        _group(
            range(0, 5),
            hook=HookType.QUESTION.value,
            observations_for=lambda i: (spec(20, 12, 6),),
        )
        + _group(
            range(5, 10),
            hook=HookType.STATEMENT.value,
            observations_for=lambda i: (spec(8, 4, 1),),
        )
    )
    return Case(
        category="balanced_cohorts",
        note="5 + 5 且三指标齐全：比较成立，`learning_ready=True`",
        window=WINDOW,
        records=records,
        expect=[
            ("ready", "is", True),
            ("reason", "==", "ready"),
            ("sample_count", "==", 10),
            ("failed_checks", "empty", None),
            ("valid_comparison_count", "==", 3),
            ("evidence_count", "==", 3),
            ("evidence_kinds", "contains", "cohort_difference"),
            # 即便允许"作为学习材料"，也不允许动 Agent。
            ("automatic_modification", "is", False),
            ("all_statements", "all_contain", "不构成任何因果结论"),
        ],
    )


def _unequal_cohorts() -> Case:
    """5 条 vs 3 条：总量够，分组不够——两组都产不出结论。"""
    records = tuple(
        _group(
            range(0, 5),
            hook=HookType.QUESTION.value,
            observations_for=lambda i: (spec(20, 12, 6),),
        )
        + _group(
            range(5, 8),
            hook=HookType.STATEMENT.value,
            observations_for=lambda i: (spec(8, 4, 1),),
        )
    )
    return Case(
        category="unequal_cohorts",
        note="5 + 3：整体样本量过了下限，但较小的那一组没过，比较不成立",
        window=WINDOW,
        records=records,
        expect=[
            ("sample_count", "==", 8),
            ("ready", "is", False),
            ("reason", "==", "invalid_comparison"),
            ("valid_comparison_count", "==", 0),
            ("comparison_count", "==", 3),
            # 不成立的原因是**两组帖子数**，不是读数缺失——这两者要分得开。
            ("invalid_reasons", "none_contain", "有读数"),
            ("invalid_reasons", "any_contain", "帖子数"),
            ("evidence_kinds", "contains", "invalid_comparison"),
        ],
    )


def _missing_metrics() -> Case:
    """6 条帖子，每条只读回一类计数：每类都够不到下限，**不补 0**。"""
    observations = (
        spec(likes=9),
        spec(likes=11),
        spec(conversation=4),
        spec(conversation=6),
        spec(amplification=2),
        spec(amplification=3),
    )
    records = tuple(
        make_record(i, observations=(item,))
        for i, item in enumerate(observations)
    )
    return Case(
        category="missing_metrics",
        note="三指标都稀疏：合计读数够，但没有任何一类达到比较下限",
        window=WINDOW,
        records=records,
        expect=[
            ("ready", "is", False),
            ("reason", "==", "missing_metrics"),
            ("sample_count", "==", 6),
            ("attention_readings", "==", 2),
            ("conversation_readings", "==", 2),
            ("amplification_readings", "==", 2),
            # 读不到就**不进分布**：若补成 0，这三类的读数会各变成 6。
            ("attention_readings", "<", 6),
            ("valid_comparison_count", "==", 0),
            ("evidence_kinds", "contains", "missing_metric"),
        ],
    )


def _different_windows() -> Case:
    """一半记录在 `1-6h`、一半在 `24-72h`：窗口不一致就不许直接比。"""
    records = tuple(
        _group(
            range(0, 5),
            hook=HookType.QUESTION.value,
            observations_for=lambda i: (spec(20, 12, 6, hours=EARLY_HOURS),),
            window=EARLY_WINDOW,
        )
        + _group(
            range(5, 10),
            hook=HookType.STATEMENT.value,
            observations_for=lambda i: (spec(8, 4, 1),),
            window=WINDOW,
        )
    )
    return Case(
        category="different_windows",
        note="两组各自的窗口不同：闸门先拒绝，报告不产出任何差异结论",
        window=WINDOW,
        records=records,
        expect=[
            ("ready", "is", False),
            ("reason", "==", "inconsistent_window"),
            ("failed_checks", "contains", "window_consistent"),
            ("sample_count", "==", 5),
            ("valid_comparison_count", "==", 0),
            ("evidence_kinds", "contains", "window_mismatch"),
            ("all_statements", "none_contain", "高于"),
        ],
    )


def _outlier() -> Case:
    """5 + 5，其中一条点赞 900。它被标记，但**不被删除**。"""
    question = (spec(10, 3, 1), spec(11, 3, 1), spec(12, 3, 1), spec(13, 3, 1), spec(900, 3, 1))
    statement = tuple(spec(10 + i, 3, 1) for i in range(5))
    records = tuple(
        make_record(i, observations=(item,))
        for i, item in enumerate(question)
    ) + tuple(
        make_record(5 + i, observations=(item,), hook=HookType.STATEMENT.value)
        for i, item in enumerate(statement)
    )
    return Case(
        category="outlier",
        note="一条 900 的观察：被标记为离群，但完整参与中位数计算",
        window=WINDOW,
        records=records,
        expect=[
            ("ready", "is", True),
            ("sample_count", "==", 10),
            ("valid_comparison_count", "==", 3),
            # 一个爆款不该让闸门关掉，也不该被悄悄删掉凑出一个"干净"的分布。
            ("automatic_modification", "is", False),
        ],
    )


def _causal_language() -> Case:
    """5 + 5，差异干净——正是最容易写成"因为……所以……"的那种数据。"""
    records = tuple(
        _group(
            range(0, 5),
            hook=HookType.QUESTION.value,
            observations_for=lambda i: (spec(30, 15, 9),),
        )
        + _group(
            range(5, 10),
            hook=HookType.STATEMENT.value,
            observations_for=lambda i: (spec(5, 2, 1),),
        )
    )
    return Case(
        category="causal_language",
        note="差异明显也不能写成因果：结论只描述观察到的分布",
        window=WINDOW,
        records=records,
        expect=[
            ("ready", "is", True),
            ("valid_comparison_count", "==", 3),
            ("all_statements", "all_contain", "这是观察到的差异，不构成任何因果结论。"),
            ("all_statements", "none_contain", "因为"),
            ("all_statements", "none_contain", "所以"),
            ("all_statements", "none_contain", "导致"),
        ],
    )


def _invalid_comparison() -> Case:
    """5 + 5，但第二组三指标里每一项有读数的都不到下限：读数不足，不比较。"""
    question = tuple(spec(20, 12, 6) for _ in range(5))
    # 第二组：2 条只有点赞，3 条只有对话。每一类在该组里都够不到 5 条读数。
    statement = (spec(likes=8), spec(likes=6)) + tuple(
        spec(conversation=n) for n in (2, 3, 4)
    )
    records = tuple(
        make_record(i, observations=(item,))
        for i, item in enumerate(question)
    ) + tuple(
        make_record(5 + i, observations=(item,), hook=HookType.STATEMENT.value)
        for i, item in enumerate(statement)
    )
    return Case(
        category="invalid_comparison",
        note="两组帖子数相同，但第二组每个指标有读数的都不到 5 条",
        window=WINDOW,
        records=records,
        expect=[
            ("ready", "is", False),
            ("reason", "==", "invalid_comparison"),
            ("sample_count", "==", 10),
            ("valid_comparison_count", "==", 0),
            ("comparison_count", "==", 3),
            ("invalid_reasons", "any_contain", "有读数"),
            ("evidence_kinds", "contains", "invalid_comparison"),
        ],
    )


#: 九组，顺序固定（工作单第二十七节点名的那九个，一个不多一个不少）。
CASES: tuple[Case, ...] = (
    _one_post(),
    _small_sample(),
    _balanced_cohorts(),
    _unequal_cohorts(),
    _missing_metrics(),
    _different_windows(),
    _outlier(),
    _causal_language(),
    _invalid_comparison(),
)

#: 类别名，顺序与 `CASES` 一致。`selftest.py` 用它核对"九类都在"。
CATEGORIES: tuple[str, ...] = (
    "one_post",
    "small_sample",
    "balanced_cohorts",
    "unequal_cohorts",
    "missing_metrics",
    "different_windows",
    "outlier",
    "causal_language",
    "invalid_comparison",
)


def case_for(category: str) -> Case:
    for case in CASES:
        if case.category == category:
            return case
    raise KeyError(category)


__all__ = [
    "CASES",
    "CATEGORIES",
    "EARLY_HOURS",
    "EARLY_WINDOW",
    "HOURS",
    "PUBLISHED_AT",
    "WINDOW",
    "Case",
    "case_for",
    "make_record",
    "spec",
]
