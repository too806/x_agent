"""反馈评估的**手写**数据（工作单第三十二节）。

六类，一类一组观察。全部合成、不碰真实 X、不读数据库——这一套的观察是
直接构造出来的 `SnapshotObservation`，所以每一组都只差**一个变量**。

## 预期是数据，不是注释

每组数据带一份 `expect`：一串 `(信号下标, 字段, 关系, 期望值)` 断言，
下标为 `None` 时表示报告层的字段。`selftest.py` 逐条核对这些断言，
**不掺任何人工判断**。写成数据而不是写在检查函数的注释里，是为了两件事：

1. "这一组应当产出什么"在报告里是**可读的**，不藏在代码里；
2. 关系是显式的（`<= 4` 而不是"被压住了"），所以哪天常数被改动，
   失败信息会直接指向"5 <= 4 不成立"，而不是一句"不符合预期"。

## 每组都成对

只看"该响的响了"的基准分不清"检测器在判"与"检测器对什么都判"。所以每一类
都配一条**反向扰动**（在 `selftest.py` 里就地构造，因为扰动往往只是改一个
数字）：把触发它的那一项改回正常值，那条结论必须消失或翻过来。

报告层可读的字段（`selftest.py` 里的 `_resolve` 实现）：

    sample_count / signal_count / signal_kinds / limitation_count
    / insufficient_present / comparative_present

信号层可读的字段：`FeedbackSignal` 的每一个字段，外加 `evidence_count`。

关系词（`selftest.py` 里实现）：

    "=="、"!="、"<"、"<="、">"、">="  —— 普通比较
    "is"                              —— 恒等（`None` 必须用这个，不能用 ==）
    "contains" / "not_contains"       —— 序列或字符串包含 / 不包含
    "empty" / "nonempty"              —— 序列为空 / 非空
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.feedback.schema import SnapshotObservation
from app.feedback.signals import MIN_SAMPLES_FOR_COMPARISON

#: 所有观察共用的一个发布时刻。**与观察时刻分开**：报告里两者的差是
#: `hours_since_publish`，而这条差是写入时算好的，不在这里重算
#: （工作单第九节的时间语义纪律）。
PUBLISHED_AT = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Case:
    """一组观察，以及它必须满足的断言。"""

    category: str
    note: str
    observations: tuple[SnapshotObservation, ...]
    #: `(信号下标或 None, 字段, 关系, 期望值)`。
    expect: list[tuple[int | None, str, str, Any]] = field(default_factory=list)


def obs(
    index: int,
    *,
    hours: float | None,
    likes: int | None = None,
    replies: int | None = None,
    reposts: int | None = None,
    quotes: int | None = None,
    post: int = 1,
) -> SnapshotObservation:
    """一条观察。`observed_at` 由 `hours` 推出来，用来盯住"两个时刻分开"。

    `hours=None` 时**不编一个时刻**：那正是"时间语义读不到"的样子，
    `observed_at` 与 `hours_since_publish` 一起留空。
    """
    return SnapshotObservation(
        snapshot_id=f"{post}-snap-{index:02d}",
        x_post_id=f"194100000000000000{post}",
        published_post_id=f"post-{post}",
        observed_at=None if hours is None else PUBLISHED_AT + timedelta(hours=hours),
        hours_since_publish=hours,
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
    )


# ---------------------------------------------------------------------------
# 一、小样本：低于下限就什么都不比
# ---------------------------------------------------------------------------

SMALL_SAMPLE = Case(
    category="small_sample",
    note=(
        f"只有 2 条观察，低于下限 {MIN_SAMPLES_FOR_COMPARISON}；"
        "应当**恰好**产出一条只带 n 的信号，不产出任何分布或比率"
    ),
    observations=(
        obs(1, hours=1.0, likes=12, replies=1, reposts=0, quotes=0),
        obs(2, hours=3.0, likes=15, replies=2, reposts=1, quotes=0),
    ),
    expect=[
        (None, "sample_count", "==", 2),
        (None, "signal_count", "==", 1),
        (None, "signal_kinds", "==", ("insufficient_sample",)),
        (None, "comparative_present", "is", False),
        (None, "insufficient_present", "is", True),
        (0, "kind", "==", "insufficient_sample"),
        (0, "sample_count", "==", 2),
        (0, "value", "==", 2.0),
        (0, "metric", "is", None),
        # 下限必须与显著性分开说，不能让人读成"统计上不显著"
        (0, "statement", "contains", "不是统计显著性判断"),
        (0, "evidence_count", "==", 2),
    ],
)


# ---------------------------------------------------------------------------
# 二、健康样本：四类分布都产出，且每条都带 n 与依据
# ---------------------------------------------------------------------------

HEALTHY_SAMPLE = Case(
    category="healthy_sample",
    note="8 条观察、四类计数齐全；注意力/对话/扩散各一条分布信号，外加一条构成",
    observations=tuple(
        obs(
            index,
            hours=hours,
            likes=likes,
            replies=replies,
            reposts=reposts,
            quotes=quotes,
        )
        for index, (hours, likes, replies, reposts, quotes) in enumerate(
            (
                (0.5, 12, 1, 0, 0),
                (2.0, 15, 2, 1, 0),
                (4.0, 18, 1, 2, 1),
                (8.0, 20, 3, 1, 0),
                (20.0, 25, 2, 3, 1),
                (30.0, 30, 4, 2, 1),
                (80.0, 40, 3, 4, 0),
                (90.0, 45, 5, 3, 1),
            ),
            start=1,
        )
    ),
    expect=[
        (None, "sample_count", "==", 8),
        (None, "signal_count", "==", 4),
        (None, "signal_kinds", "contains", "attention"),
        (None, "signal_kinds", "contains", "conversation"),
        (None, "signal_kinds", "contains", "amplification"),
        (None, "signal_kinds", "contains", "share"),
        (None, "comparative_present", "is", True),
        (0, "kind", "==", "attention"),
        (0, "value", "==", 22.5),
        (0, "sample_count", "==", 8),
        (0, "evidence_count", "==", 8),
        # 每条分布结论都必须自带"这是观察、不是因果"的口径
        (0, "statement", "contains", "不构成任何因果结论"),
        (0, "comparison_group", "is", None),
        (0, "confidence", "is", None),
        (3, "kind", "==", "share"),
        # 注意力占互动总量 0.83 上下：22.5 / (22.5+3+2)
        (3, "value", ">", 0.7),
    ],
)


# ---------------------------------------------------------------------------
# 三、缺字段：读不到就记"读不到"，绝不按 0 计入
# ---------------------------------------------------------------------------

MISSING_METRIC = Case(
    category="missing_metric",
    note=(
        "8 条观察里有 3 条读不到点赞数；注意力分布只能在有读数的 5 条上算，"
        "并且必须**明说**有 3 条缺失，而不是把那 3 条当成 0 个赞"
    ),
    observations=(
        obs(1, hours=1.0, likes=12, replies=1, reposts=0, quotes=0),
        obs(2, hours=2.0, likes=15, replies=2, reposts=1, quotes=0),
        obs(3, hours=3.0, likes=None, replies=1, reposts=2, quotes=1),
        obs(4, hours=4.0, likes=None, replies=3, reposts=1, quotes=0),
        obs(5, hours=5.0, likes=25, replies=2, reposts=3, quotes=1),
        obs(6, hours=6.0, likes=None, replies=4, reposts=2, quotes=1),
        obs(7, hours=7.0, likes=40, replies=3, reposts=4, quotes=0),
        obs(8, hours=8.0, likes=45, replies=5, reposts=3, quotes=1),
    ),
    expect=[
        (None, "sample_count", "==", 8),
        (None, "signal_count", "==", 5),
        (None, "signal_kinds", "contains", "missing_metric"),
        (None, "signal_kinds", "contains", "attention"),
        # 注意力信号只在 5 条有读数的观察上成立，不是 8 条
        (0, "kind", "==", "attention"),
        (0, "sample_count", "==", 5),
        (0, "evidence_count", "==", 5),
        # 缺失信号排在最后（`build_signals` 的顺序固定：三类分布 → 构成 → 缺失），
        # 所以它是第 5 条。缺失不是 0：它的 value 是 None，不是 3 也不是 0。
        (4, "kind", "==", "missing_metric"),
        (4, "metric", "==", "likes"),
        (4, "value", "is", None),
        (4, "sample_count", "==", 3),
        (4, "statement", "contains", "不按 0 计入"),
    ],
)


# ---------------------------------------------------------------------------
# 四、多个时间档：分档只由存下来的小时数决定
# ---------------------------------------------------------------------------

MULTIPLE_WINDOWS = Case(
    category="multiple_windows",
    note=(
        "8 条观察落在 4 个不同的时间档（0-1h / 1-6h / 6-24h / 24-72h）；"
        "报告必须把覆盖到的档说清楚，而不是只印一堆数字"
    ),
    observations=(
        obs(1, hours=0.5, likes=5, replies=1, reposts=0, quotes=0),
        obs(2, hours=0.9, likes=6, replies=1, reposts=1, quotes=0),
        obs(3, hours=2.0, likes=9, replies=2, reposts=1, quotes=1),
        obs(4, hours=5.5, likes=11, replies=2, reposts=2, quotes=1),
        obs(5, hours=8.0, likes=14, replies=3, reposts=2, quotes=1),
        obs(6, hours=20.0, likes=18, replies=3, reposts=3, quotes=2),
        obs(7, hours=30.0, likes=21, replies=4, reposts=3, quotes=2),
        obs(8, hours=60.0, likes=24, replies=4, reposts=4, quotes=2),
    ),
    expect=[
        (None, "sample_count", "==", 8),
        (None, "signal_count", "==", 4),
        (None, "limitation_count", ">", 4),
    ],
)


# ---------------------------------------------------------------------------
# 五、高注意力、低对话：两类行为各自成条，不许合成一个"好坏"
# ---------------------------------------------------------------------------

HIGH_ATTENTION_LOW_CONVERSATION = Case(
    category="high_attention_low_conversation",
    note=(
        "点赞很高（每条约 400），对话与扩散接近零；"
        "注意力占比应当接近 1，对话占比接近 0——而报告里**不该**出现任何"
        "「这样的帖子更好/更差」的判断，因为点赞不等于质量"
    ),
    observations=tuple(
        obs(i, hours=float(i), likes=400 + i, replies=0, reposts=0, quotes=0)
        for i in range(1, 9)
    ),
    expect=[
        (None, "sample_count", "==", 8),
        (None, "signal_kinds", "contains", "share"),
        (None, "signal_kinds", "contains", "conversation"),
        (0, "kind", "==", "attention"),
        (0, "value", ">=", 400),
        # 构成信号的度量名是 engagement_share，值是注意力占比
        (3, "kind", "==", "share"),
        (3, "value", ">", 0.99),
        (None, "limitation_count", ">", 4),
    ],
)


# ---------------------------------------------------------------------------
# 六、低注意力、高对话：同一套公式，翻过来
# ---------------------------------------------------------------------------

LOW_ATTENTION_HIGH_CONVERSATION = Case(
    category="low_attention_high_conversation",
    note=(
        "点赞很低（每条约 3），回复+引用很高（每条约 40）；"
        "注意力占比应当接近 0——同一套公式在反方向上的样子"
    ),
    observations=tuple(
        obs(i, hours=float(i), likes=3, replies=20, reposts=0, quotes=20)
        for i in range(1, 9)
    ),
    expect=[
        (None, "sample_count", "==", 8),
        (0, "kind", "==", "attention"),
        (0, "value", "==", 3.0),
        (1, "kind", "==", "conversation"),
        (1, "value", "==", 40.0),
        (3, "kind", "==", "share"),
        (3, "value", "<", 0.1),
    ],
)


#: 工作单第三十二节点名的六类，顺序即报告顺序。
CASES: tuple[Case, ...] = (
    SMALL_SAMPLE,
    HEALTHY_SAMPLE,
    MISSING_METRIC,
    MULTIPLE_WINDOWS,
    HIGH_ATTENTION_LOW_CONVERSATION,
    LOW_ATTENTION_HIGH_CONVERSATION,
)

#: 六类的名字。`selftest.py` 会核对这里没有漏项或多项——工作单点了名的那几类
#: 必须真的在，否则"覆盖了六类"只是一句自述。
CATEGORIES: tuple[str, ...] = (
    "small_sample",
    "healthy_sample",
    "missing_metric",
    "multiple_windows",
    "high_attention_low_conversation",
    "low_attention_high_conversation",
)
