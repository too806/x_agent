"""枚举取值 → 中文说明。**一处定义，多处使用**。

这些表原本住在 `app/agent/prompts.py` 里，因为当时唯一的用处是渲染给模型看的
`{{STYLE}}` 块。Phase 5B.2 的 Run Detail 要展示同一组取值，于是出现了一个
必须当场解决的问题：**再抄一份，还是把它提出来？**

抄一份看起来更省事，但两份标签表会漂——而漂移的方向恰好是最难发现的那种：
模型按"先给判断，再给理由"写，界面上显示"对比 A 与 B"，人于是拿着一个
不存在的目标去评判这条稿子。而且这种不一致**不会报错**，只会让评审结论
莫名地对不上。所以提到这里，两边都从这里取。

写成表而不是 f-string 里的 if 链：加一个分类法成员时，"忘了给它中文说明"
会表现为 prompt 与界面里出现一个英文蛇形词，而不是一个报错——表让这件事
一眼可见。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DENSITY_LABELS",
    "ENDING_LABELS",
    "EVIDENCE_LABELS",
    "HOOK_LABELS",
    "STANCE_LABELS",
    "STRUCTURE_LABELS",
    "TONE_LABELS",
    "density_label",
    "ending_label",
    "evidence_label",
    "hook_label",
    "label",
    "stance_label",
    "structure_label",
    "tone_label",
]


def label(value: Any) -> str:
    """枚举 → 它的字符串值。非枚举（如已经取过 .value 的字符串）原样返回。"""
    return str(getattr(value, "value", value))


STRUCTURE_LABELS: dict[str, str] = {
    "opinion_first": "先给判断，再给理由",
    "fact_first": "先摆事实/数据，再引出判断",
    "question_answer": "提出一个问题，然后回答它",
    "contrast": "对比 A 与 B，落在一个判断上",
    "how_to": "步骤/清单式，教怎么做",
    "case_review": "一个具体案例的复盘",
    "counterintuitive": "先立一个常见看法，再反驳它",
    "observation": "描述现象/趋势，少下结论",
    "data_reading": "围绕一组数字展开",
    "prediction": "基于现状推演未来",
    "unclassified": "不指定结构",
}

HOOK_LABELS: dict[str, str] = {
    "question": "用一个问题开头",
    "statistic": "用一个数字/数据开头",
    "statement": "用一句直接陈述开头",
    "list": "用「几条」式的清单预告开头",
    "story": "用一个具体事件/场景开头",
    "contrast": "用一个反差/意外开头",
    "quote": "用一句原话或具体指称开头",
    "unclassified": "不指定开头方式",
}

DENSITY_LABELS: dict[str, str] = {
    "low": "低（少堆事实，把一条说透）",
    "medium": "中",
    "high": "高（多条事实交替支撑）",
}

STANCE_LABELS: dict[str, str] = {
    "soft": "弱（陈述观察，不下强判断）",
    "moderate": "中",
    "strong": "强（可以直接下判断）",
}

EVIDENCE_LABELS: dict[str, str] = {
    "optional": "可选（材料不足，不要硬编）",
    "required": "必需（每个判断都要有出处）",
    "strict": "严格（逐条对应，且要写明依据）",
}

TONE_LABELS: dict[str, str] = {
    "neutral": "中性",
    "direct": "直接",
    "analytical": "分析型",
    "conversational": "口语化",
}

ENDING_LABELS: dict[str, str] = {
    "conclusion": "给一个明确结论",
    "question": "用一个问句收尾",
    "call": "用行动号召收尾",
    "observation": "只做观察陈述，不强行收结论",
}


def _lookup(table: dict[str, str], value: Any) -> str:
    """查表。**查不到时回落到取值本身**，不是抛异常也不是空串。

    recipe 是运行期持久化的数据，它可能来自更早的一版分类法。一个当时的
    合法取值现在查不到中文，应当原样显示出来（人一眼能看出"这是个我不认识
    的取值"），而不是让整个 Run Detail 打不出来。
    """
    raw = label(value)
    return table.get(raw, raw)


def structure_label(value: Any) -> str:
    return _lookup(STRUCTURE_LABELS, value)


def hook_label(value: Any) -> str:
    return _lookup(HOOK_LABELS, value)


def density_label(value: Any) -> str:
    return _lookup(DENSITY_LABELS, value)


def stance_label(value: Any) -> str:
    return _lookup(STANCE_LABELS, value)


def evidence_label(value: Any) -> str:
    return _lookup(EVIDENCE_LABELS, value)


def tone_label(value: Any) -> str:
    return _lookup(TONE_LABELS, value)


def ending_label(value: Any) -> str:
    return _lookup(ENDING_LABELS, value)
