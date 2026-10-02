"""反馈层的确定性度量：由观察计数派生出来的一切数（Phase 5B.4）。

## 这个模块最重要的一条纪律：未知是 `None`，不是 `0`

工作单第十、三十一节把这条写成硬要求。一次抓取里 X 没返回 `like_count`，
我们**没有观测到 0 个赞**——我们什么都没观测到。把这种情况记成 0，
下游读到的就是一个"互动量为零"的结论，而它与"这条读不到"含义相反。
所以本模块里每一个可能算不出来的数都是 `int | None` / `float | None`，
并且**没有一处默认值写成 0**。

由此推出两条具体规则：

1. **求和是"全有才求和"**。`engagement_total` 只要四项里有任意一项未知就是
   `None`——把已知的三项加起来，会把"未知"变成一个更小的、看起来真实的数字。
2. **比率的分母缺失或 <= 0 → `None`**。分母为 0 时"比例是多少"没有被定义，
   而 0.0 会被读成"测出来是零"。

## 为什么是纯函数

同 `app/agent/viewpoint.py`：没有 IO、没有随机、没有 `datetime.now()`、
不读配置。这些数**不落库**——它们完全由观察行推出来，重算逐位相同，
存一份只会多一个将来会漂移的来源（同 `style_sample` 那条理由，
见迁移 `c3d8a45e7f21`）。快照里唯一允许存下来的是 `hours_since_publish`，
因为它**不能**从快照行自身重算。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 观察时刻距发布时刻的分档边界（小时），工作单第七节。
#: 分档是**纯函数**：只要 `hours_since_publish` 存下来了，读的时候随时能分，
#: 不需要一个有状态的调度器按时去"采样第 1 小时"。
WINDOW_BOUNDARY_HOURS: tuple[float, ...] = (1, 6, 24, 72)

#: 与 `WINDOW_BOUNDARY_HOURS` 一一对应，末档是开区间。
WINDOW_LABELS: tuple[str, ...] = ("0-1h", "1-6h", "6-24h", "24-72h", ">72h")


@dataclass(frozen=True)
class Metrics:
    """一条观察的四类公开计数（外加 v1 从不采集的两项）。

    四个主计数一律可空。`impressions` / `bookmarks` 留在形状里**不是因为
    我们读到了它们**，而是给将来的来源留一个明显的落点；v1 的采集只请求
    `public_metrics` 里的公开计数，这两项恒为 `None`。
    """

    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    impressions: int | None = None
    bookmarks: int | None = None

    @classmethod
    def from_observation(cls, observation) -> "Metrics":
        """从一条观察行（`SnapshotObservation` 或模型行）取计数。

        鸭子类型、不做 import：这个模块是纯的，不该知道持久化的形状。
        """
        return cls(
            likes=observation.likes,
            replies=observation.replies,
            reposts=observation.reposts,
            quotes=observation.quotes,
            impressions=observation.impressions,
            bookmarks=observation.bookmarks,
        )


def _sum_or_none(*parts: int | None) -> int | None:
    """**全有才求和**：任一未知 → `None`。

    见模块开头第 1 条。这个函数是 `engagement_total` 与 `conversation`
    共用的地方，也是"部分已知不许冒充总量"这条纪律的唯一落点。
    """
    if any(part is None for part in parts):
        return None
    return sum(parts)  # type: ignore[arg-type]  # 上面已排除 None


def rate(numerator: int | float | None, denominator: int | float | None) -> float | None:
    """`numerator / denominator`，分母缺失或 <= 0 时返回 `None`。

    **不返回 0.0**。分母为 0 时比率没有定义；吐一个 0 出去，
    报告里就会多一条"占比为零"的假结论。
    """
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


# --- 三类互动：把"点赞多"与"有人说得好"分开（工作单第十二节） ---------------
#
# 四个计数不是同一个东西，不能先合成一个"热度"再讨论。工作单第十二节把
# 它们分成三类，本模块照此提供三个**独立**的量：
#
#   attention      = likes                  （多少人看见了 / 顺手点了一下）
#   conversation   = replies + quotes       （有多少人真的开口了）
#   amplification  = reposts                （有多少人愿意把它带给别人）
#
# 三者没有被合成任何加权总分——那正是 §12 禁止的"用一个数代表好坏"。


def attention(metrics: Metrics) -> int | None:
    return metrics.likes


def conversation(metrics: Metrics) -> int | None:
    return _sum_or_none(metrics.replies, metrics.quotes)


def amplification(metrics: Metrics) -> int | None:
    return metrics.reposts


def engagement_total(metrics: Metrics) -> int | None:
    """四类互动之和；任一未知 → `None`（见 `_sum_or_none`）。"""
    return _sum_or_none(
        metrics.likes, metrics.replies, metrics.reposts, metrics.quotes
    )


def engagement_rate(metrics: Metrics) -> float | None:
    """互动 / 曝光。**v1 恒为 `None`**。

    分母 `impressions` 从未被采集（v1 只请求公开计数），所以这个函数现在
    必然返回 `None`。留着它而不是删掉：将来某一天有了曝光来源，
    它有一个显然的落点，而不是临时再发明一个名字。**绝不**用别的东西
    顶替分母——那会造出一个口径不同、却长得一样的数。
    """
    return rate(engagement_total(metrics), metrics.impressions)


def reply_rate(metrics: Metrics) -> float | None:
    return rate(metrics.replies, engagement_total(metrics))


def repost_rate(metrics: Metrics) -> float | None:
    return rate(metrics.reposts, engagement_total(metrics))


def quote_rate(metrics: Metrics) -> float | None:
    return rate(metrics.quotes, engagement_total(metrics))


def attention_share(metrics: Metrics) -> float | None:
    return rate(attention(metrics), engagement_total(metrics))


def conversation_share(metrics: Metrics) -> float | None:
    return rate(conversation(metrics), engagement_total(metrics))


def amplification_share(metrics: Metrics) -> float | None:
    return rate(amplification(metrics), engagement_total(metrics))


def window_bucket(hours_since_publish: float | None) -> str | None:
    """观察落在发布后的哪个时间档。

    `None` 进 `None` 出。**负数也返回 `None`**：观察时刻早于发布时刻说明
    数据本身不自洽（`published_at` 或 `observed_at` 有一个是错的），
    硬塞进 `0-1h` 会把一个数据问题伪装成一条"发布后第一小时内"的正常观察。
    """
    if hours_since_publish is None:
        return None
    if hours_since_publish < 0:
        return None
    for boundary, label in zip(WINDOW_BOUNDARY_HOURS, WINDOW_LABELS):
        if hours_since_publish < boundary:
            return label
    return WINDOW_LABELS[-1]


__all__ = [
    "WINDOW_BOUNDARY_HOURS",
    "WINDOW_LABELS",
    "Metrics",
    "amplification",
    "amplification_share",
    "attention",
    "attention_share",
    "conversation",
    "conversation_share",
    "engagement_rate",
    "engagement_total",
    "quote_rate",
    "rate",
    "reply_rate",
    "repost_rate",
    "window_bucket",
]
