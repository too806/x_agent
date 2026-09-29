"""指标值的约定：**算不出来就说算不出来**。

整套评估里最容易被做坏的一件事，是让缺失的数据悄悄变成 0。一旦"无法计算"
和"计算结果是 0"在报告里长得一样，所有平均值都会被拉低、所有通过率都会被
拉高，而且——这是最糟的部分——**看不出来**。

所以这里把 `UNAVAILABLE` 定成一个显式的字符串哨兵，并要求：

- 任何比率在分母为 0 时返回 `UNAVAILABLE`，**不是** 0.0；
- 任何"数据里没有这个字段"的指标返回 `UNAVAILABLE`，不是默认值；
- `UNAVAILABLE` 不参与聚合：`mean()` 会把它们摘掉，并在返回里报告
  "基于几个样本"。

哨兵值用字符串 "unavailable" 而不是 None，是因为它要进 JSON、要印在终端、
要被人贴进 issue。`null` 在这三个地方都容易被当成"没填"，而
"unavailable" 一眼就知道是评估器的判断。
"""

from __future__ import annotations

from typing import Any, Iterable

UNAVAILABLE = "unavailable"

#: 一个指标值的类型：数字、布尔，或"算不出来"。
MetricValue = float | int | bool | str


def rate(numerator: int, denominator: int) -> float | str:
    """比率。分母为 0 时返回 UNAVAILABLE。

    这是本模块存在的主要理由。`n / d if d else 0.0` 是这类代码里最常见的一行，
    而它在 d == 0 时给出的 0.0 会让"一次评审都没发生"看起来像"评审通过率 0%"。
    """
    if denominator <= 0:
        return UNAVAILABLE
    return numerator / denominator


def mean(values: Iterable[Any]) -> float | str:
    """算术平均，跳过 UNAVAILABLE 与 None。全空时返回 UNAVAILABLE。

    **只**返回一个数，不返回样本量——需要样本量的调用方用 `mean_with_n()`。
    两件事分开是有意的：绝大多数调用点只关心值，硬塞一个元组进 JSON
    会让报告多出一堆没人读的 `_n` 字段。
    """
    return mean_with_n(values)[0]


def mean_with_n(values: Iterable[Any]) -> tuple[float | str, int]:
    """平均 + 有效样本量。样本量为 0 时值一定是 UNAVAILABLE。"""
    usable = [v for v in values if v is not None and v != UNAVAILABLE]
    if not usable:
        return UNAVAILABLE, 0
    return sum(usable) / len(usable), len(usable)


def is_available(value: Any) -> bool:
    return value is not None and value != UNAVAILABLE


def ratio_available(values: Iterable[Any]) -> float | str:
    """一串布尔里 True 的占比。空集合返回 UNAVAILABLE。"""
    items = [bool(v) for v in values]
    return rate(sum(items), len(items))


def pretty(value: Any) -> str:
    """把指标值渲染成人读的字符串。

    布尔印成 pass/fail 而不是 True/False：报告里的布尔几乎全部是
    "这条规则过了没有"，而 True/False 在这个语境下要人多想一秒。
    """
    if value is None or value == UNAVAILABLE:
        return "unavailable"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def percent(value: Any) -> str:
    """比率渲染成百分比。不可用时原样返回。"""
    if value is None or value == UNAVAILABLE:
        return "unavailable"
    if isinstance(value, bool):
        return "100%" if value else "0%"
    return f"{float(value) * 100:.0f}%"
