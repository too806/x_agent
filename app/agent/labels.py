"""Agent 域枚举取值 → 中文说明。**一处定义，多处使用**。

与 `app/style/labels.py` 同一条理由：这些取值要同时出现在**给模型看的 prompt**
与**给人看的 Run Detail** 里。抄一份看起来更省事，但两份表会漂，而漂移的方向
恰好是最难发现的那种——模型按"这个判断只针对某个细分"写，界面上显示"整个行业"，
人于是拿着一个不存在的目标去评判这条稿子。而且这种不一致**不会报错**。

## 为什么不并进 app/style/labels.py

那里的每一项都是**写作参数**（结构、语气、句长档位）。这里的每一项是
**认识论状态**（这个判断有多笃定、它声称管到哪一步）。两者会同时出现在同一个
Run Detail 里，混在一张表里读的人会以为它们是同一层的东西。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CLAIM_SCOPE_LABELS",
    "CONTEXT_ORIGIN_LABELS",
    "VIEWPOINT_STANCE_LABELS",
    "claim_scope_label",
    "context_origin_label",
    "viewpoint_stance_label",
]


def _label(value: Any) -> str:
    """枚举 → 它的字符串值。非枚举（如已经取过 .value 的字符串）原样返回。"""
    return str(getattr(value, "value", value))


#: 判断的强度（工作单第十节）。注意措辞落在"判断有多笃定"上，
#: 不要写成"立场有多强"——那会与 `app/style/labels.py::STANCE_LABELS`
#: （表达有多硬）看起来是同一件事。
VIEWPOINT_STANCE_LABELS: dict[str, str] = {
    "strong": "明确判断（证据充分，立场鲜明）",
    "moderate": "倾向性判断（有依据，但保留余地）",
    "cautious": "谨慎判断（证据有限，明确说明条件）",
    "uncertain": "不足以形成判断（材料还撑不起来）",
}

#: 判断声称自己管到哪一步（工作单第二十八、二十九节）。
CLAIM_SCOPE_LABELS: dict[str, str] = {
    "fact": "只是解释一条观察",
    "case": "某个具体案例 / 某次发布 / 某家公司",
    "segment": "某个细分领域 / 某类产品 / 某段时间",
    "industry": "整个行业 / 一个市场",
    "society": "整个社会 / 一个时代",
}


def viewpoint_stance_label(value: Any) -> str:
    """取值 → 中文。认不出来的取值原样回退成它的字符串值。"""
    key = _label(value)
    return VIEWPOINT_STANCE_LABELS.get(key, key)


def claim_scope_label(value: Any) -> str:
    """取值 → 中文。认不出来的取值原样回退成它的字符串值。"""
    key = _label(value)
    return CLAIM_SCOPE_LABELS.get(key, key)


#: 背景的来源（工作单第五节）。措辞刻意写成**完整的一句限制**而不是一个名词，
#: 因为它进 prompt 是要被执行成一条约束的：看到"用户前提"就该知道
#: "这可以拿来起头，但不可以拿来当论证"。
CONTEXT_ORIGIN_LABELS: dict[str, str] = {
    "fact": "外部事实（材料里查得到出处）",
    "premise": "用户前提（来自本次目标，不是已证实的公开事实）",
    "inference": "Agent 推断（你自己的合理推断，不是材料里的话）",
}


def context_origin_label(value: Any) -> str:
    """取值 → 中文。认不出来的取值原样回退成它的字符串值。"""
    key = _label(value)
    return CONTEXT_ORIGIN_LABELS.get(key, key)
