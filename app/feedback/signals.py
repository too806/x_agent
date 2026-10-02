"""从观察里抽出可解释信号，并把"不许说因果"变成一条会报错的检查。

工作单第三、十九、三十一节把这条写成硬要求，而且它是最容易在措辞上失守的
一条——数据本身只说"这两件事同时出现"，报告里写成"因为 A 所以 B"，
下游（以及将来某一天真的会去调参的那个版本）就会把一次巧合当成一条规律。

所以这里做的不是"提醒自己注意措辞"，而是：

1. 所有 `statement` 由**固定的模板**生成，模板里没有一个因果词；
2. 生成后一律过 `assert_no_causal_claim`，命中就是一个异常。

于是"不小心写成因果句"这件事在代码层面直接失败，而不是靠评审者的眼睛。

## 另一条同样要防的：把观察写成指令

工作单第四十二节禁止 Writer 拿到"这种帖子拿了 300 赞"这类可以被当成目标的
说法。这条的检查同样做成函数（`find_instruction_language`），
用在渲染给 Writer 的那段文本上。

## 这个模块不知道的事情

- 不知道一条帖子"质量好不好"。点赞不等于质量（工作单第十二节），
  所以这里没有任何综合分、没有排名、没有"最佳/最差"。
- 不知道"什么样的帖子互动高"。那需要一个基线，而 v1 还没有自己的基线，
  于是所有信号的 `comparison_group` 恒为 `None`，只描述分布。
- 不知道两个数之间有没有因果关系，也不推测。
"""

from __future__ import annotations

import statistics
from typing import Iterable, Sequence

from app.feedback.metrics import (
    Metrics,
    amplification,
    attention,
    conversation,
    engagement_total,
    rate,
)
from app.feedback.schema import FeedbackSignal, FeedbackSignalKind, SnapshotObservation

#: 低于这个样本量就不产出**任何**分组比较结论（工作单第十七节）。
#:
#: 它是**下限**，不是显著性判断——工作单要求把这个区别写在明面上，
#: 免得一个拍出来的数字被读成"统计上显著"。5 是我们对"至少有五个不同观察
#: 才看得出一个分布长什么样"的取值，工作单没有指定数值，这是本阶段的一个
#: 显式选择。
MIN_SAMPLES_FOR_COMPARISON = 5

#: 因果措辞的封闭标记表（中英双语）。
#:
#: 这是一张**检查表**，不是语义分析——它只能抓我们自己写出来的句子。
#: 这恰好够用：`statement` 是代码生成的，所以命中必然是编程错误，
#: 而不是"这段自然语言碰巧像因果句"。
CAUSAL_MARKERS: tuple[str, ...] = (
    # 中文：因果连词、断言性动词、归因动结构
    "因为",
    "所以",
    "因此",
    "由于",
    "导致",
    "造成",
    "使得",
    "促使",
    "引发",
    "带来",
    "证明了",
    "证明是",
    "归因于",
    "之所以",
    # 英文
    "because",
    "therefore",
    "due to",
    "caused",
    "causes",
    "proves",
    "proven",
    "leads to",
    "lead to",
    "results in",
    "resulted in",
    "drives",
    "drive more",
    "attributed to",
)

#: 指令性措辞的封闭标记表。用在**渲染给 Writer 的文本**上（工作单第四十二节）：
#: 反馈是"我看到过去发生了什么"，不是"下次你要怎么写"。
INSTRUCTION_MARKERS: tuple[str, ...] = (
    "你应该",
    "你们应该",
    "建议你",
    "建议下次",
    "下次要",
    "下次写",
    "务必",
    "一定要",
    "请写",
    "照抄",
    "模仿",
    "学习这种",
    "借鉴这条",
    "you should",
    "next time",
    "make sure to",
    "must write",
    "imitate",
    "copy this",
)


class CausalLanguageError(ValueError):
    """一段本该是观察性描述的文本里出现了因果措辞。"""


class InstructionalLanguageError(ValueError):
    """一段本该是观察性描述的文本里出现了指令性措辞。"""


def _normalized(text: str) -> str:
    """查找标记前统一大小写。

    只做 `casefold`，不做别的归一化：过度的等价改写会让"哪一句被命中"
    变得不好解释，而这张表的价值恰恰在于命中哪些词一目了然。
    """
    return text.casefold()


def find_causal_language(text: str) -> list[str]:
    """返回文本里命中的因果标记（去重，按在表里的顺序）。"""
    if not text:
        return []
    lowered = _normalized(text)
    return [marker for marker in CAUSAL_MARKERS if marker in lowered]


def find_instruction_language(text: str) -> list[str]:
    """返回文本里命中的指令性标记（去重，按在表里的顺序）。"""
    if not text:
        return []
    lowered = _normalized(text)
    return [marker for marker in INSTRUCTION_MARKERS if marker in lowered]


def assert_no_causal_claim(text: str, *, where: str = "signal statement") -> None:
    """命中因果措辞即抛错。

    存心做成异常而不是日志：这些句子的模板就在同一个文件里，
    出现因果词只有一种解释——有人改了模板。让它在测试里立刻失败，
    比让它悄悄进入一份发给 Writer 的摘要要便宜得多。
    """
    hits = find_causal_language(text)
    if hits:
        raise CausalLanguageError(f"{where} 出现因果措辞 {hits!r}：{text!r}")


def assert_observational(text: str, *, where: str = "feedback context") -> None:
    """命中指令性措辞即抛错（工作单第四十二节）。"""
    hits = find_instruction_language(text)
    if hits:
        raise InstructionalLanguageError(f"{where} 出现指令性措辞 {hits!r}：{text!r}")


# --- 分布的小工具 -----------------------------------------------------------


def _fmt(value: float | None) -> str:
    """整数就按整数印，方便人读（中位数可能落在两个整数中间）。"""
    if value is None:
        return "未知"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _distribution(values: Sequence[float]) -> tuple[float, float, float] | None:
    """(中位数, 最小, 最大)；空序列返回 `None`（不是 (0,0,0)）。"""
    if not values:
        return None
    return (statistics.median(values), min(values), max(values))


def _distribution_signal(
    kind: FeedbackSignalKind,
    *,
    label: str,
    metric: str,
    values: Sequence[float],
    window: str | None,
    evidence: tuple[str, ...],
) -> FeedbackSignal | None:
    stats = _distribution(values)
    if stats is None:
        return None
    median, low, high = stats
    n = len(values)
    statement = (
        f"观察到 {n} 条观察的{label}分布：中位数 {_fmt(median)}，"
        f"最小 {_fmt(low)}，最大 {_fmt(high)}。"
        "这是观察到的分布，不构成任何因果结论。"
    )
    return FeedbackSignal(
        kind=kind,
        statement=statement,
        metric=metric,
        value=median,
        sample_count=n,
        evidence=evidence,
        window=window,
    )


#: 四类计数里，每一项在"缺字段"信号里的中文名。
_COUNT_LABELS: tuple[tuple[str, str], ...] = (
    ("likes", "点赞数"),
    ("replies", "回复数"),
    ("reposts", "转发数"),
    ("quotes", "引用数"),
)


def build_signals(
    observations: Sequence[SnapshotObservation],
    *,
    min_samples: int = MIN_SAMPLES_FOR_COMPARISON,
) -> tuple[FeedbackSignal, ...]:
    """把一批观察变成信号。

    ## 三条出口，顺序固定

    1. **没有观察 → 没有信号。** 一条不带任何依据的信号违反可追溯性
       （工作单第二十五节），所以这里返回空元组，而不是一条"暂无数据"的信号。
    2. **样本量 < `min_samples` → 恰好一条 `INSUFFICIENT_SAMPLE`。**
       它只带 n，不带任何比较、比率或排名（工作单第十七节）。
    3. 否则：三类互动各一条分布信号（该度量有读数才发），
       比率齐全时再加一条 `SHARE`，最后按需附 `MISSING_METRIC`。

    **每一档都是"有数据才发"**：某类计数全部缺失时不会生成一条 value 为
    `None` 的信号——那看起来像一条结论，实际什么都没有。
    """
    if not observations:
        return ()

    ids = tuple(o.snapshot_id for o in observations)
    n = len(observations)

    if n < min_samples:
        # 把"下限"与"显著性"分开说清楚：下限是我们自己定的，不是统计结论。
        statement = (
            f"当前可用的观察只有 {n} 条，低于比较下限 {min_samples}，"
            "本期不产出任何分组比较或比率结论。"
            "这个下限是采集量的门槛，不是统计显著性判断。"
        )
        return (
            FeedbackSignal(
                kind=FeedbackSignalKind.INSUFFICIENT_SAMPLE,
                statement=statement,
                metric=None,
                value=float(n),
                sample_count=n,
                evidence=ids,
            ),
        )

    signals: list[FeedbackSignal] = []
    metrics = [Metrics.from_observation(o) for o in observations]

    for kind, label, metric_name, extractor in (
        (FeedbackSignalKind.ATTENTION, "点赞数（注意力）", "likes", attention),
        (FeedbackSignalKind.CONVERSATION, "对话量（回复+引用）", "conversation", conversation),
        (FeedbackSignalKind.AMPLIFICATION, "转发数（扩散）", "reposts", amplification),
    ):
        pairs = [
            (o, extractor(m))
            for o, m in zip(observations, metrics)
            if extractor(m) is not None
        ]
        signal = _distribution_signal(
            kind,
            label=label,
            metric=metric_name,
            values=[value for _, value in pairs],
            window=None,
            evidence=tuple(o.snapshot_id for o, _ in pairs),
        )
        if signal is not None:
            signals.append(signal)

    # 比率只在四类计数**全部齐全**的观察上算：分母是一部分、分子是另一部分，
    # 算出来的"占比"没有意义（`rate` 本身也会在分母缺失时返回 None，
    # 这里提前筛一遍是为了让 sample_count 诚实反映"依据了几条观察"）。
    total = [engagement_total(m) for m in metrics]
    share_pairs = [
        (o, t, attention(m), conversation(m), amplification(m))
        for o, m, t in zip(observations, metrics, total)
        if t is not None
    ]
    shares = [
        (
            rate(att, t),
            rate(conv, t),
            rate(amp, t),
        )
        for _, t, att, conv, amp in share_pairs
    ]
    shares = [s for s in shares if all(part is not None for part in s)]
    if shares:
        att_median = statistics.median([s[0] for s in shares])
        conv_median = statistics.median([s[1] for s in shares])
        amp_median = statistics.median([s[2] for s in shares])
        statement = (
            f"在 {len(shares)} 条四类计数齐全的观察中，"
            f"注意力占互动总量的比例中位数为 {_fmt(att_median)}，"
            f"对话为 {_fmt(conv_median)}，扩散为 {_fmt(amp_median)}。"
            "这是观察到的构成，不构成任何因果结论。"
        )
        signals.append(
            FeedbackSignal(
                kind=FeedbackSignalKind.SHARE,
                statement=statement,
                metric="engagement_share",
                value=att_median,
                sample_count=len(shares),
                evidence=tuple(o.snapshot_id for o, *_ in share_pairs),
            )
        )

    # 缺字段：如实记下"有几条读不到某项"，让读者知道上面的分布是在
    # 一个**子集**上算的，而不是有意略过。
    for field, label in _COUNT_LABELS:
        missing = [o.snapshot_id for o in observations if getattr(o, field) is None]
        if not missing:
            continue
        statement = (
            f"有 {len(missing)}/{n} 条观察缺少{label}（来源未返回该字段），"
            "这些值记为未知，不按 0 计入任何统计。"
        )
        signals.append(
            FeedbackSignal(
                kind=FeedbackSignalKind.MISSING_METRIC,
                statement=statement,
                metric=field,
                value=None,
                sample_count=len(missing),
                evidence=tuple(missing),
            )
        )

    return tuple(signals)


__all__ = [
    "CAUSAL_MARKERS",
    "INSTRUCTION_MARKERS",
    "MIN_SAMPLES_FOR_COMPARISON",
    "CausalLanguageError",
    "InstructionalLanguageError",
    "assert_no_causal_claim",
    "assert_observational",
    "build_signals",
    "find_causal_language",
    "find_instruction_language",
]
