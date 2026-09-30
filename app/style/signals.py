"""候选正文的**确定性** AI 味辅助信号（Phase 5B.2 工作单第二十五节）。

## 它不是"AI 检测器"

工作单第二十五节的要求是：在既有的 `generic_ai_style`（一次 LLM 打分）之外，
补上**确定性的辅助信号**——过度总结、模板化开头、空洞连接词、不必要的
"首先/其次/最后"、虚假权威措辞、重复结论。

这些词表**不能**判定一段文字是 AI 写的：人也会用"首先"、也会写"事实上"。
它们能回答的是"这段文字里有哪些**可以复算的**痕迹"——而"可复算"正是
它们相对一次 LLM 打分的全部价值：同一条稿子今天和三个月后算出同一个数，
可以在报告里被引用、被质疑时重新核对。

所以它是**辅助**，不是门禁，也**绝不混进 LLM 自己的分数**：把词表计数
掺进 `generic_ai_style` 会让一个可复现的量与一个不可复现的量合成一个
既不能复算也不能追问的数。

## 不落库、不进 state

它是 `content` 的纯函数，需要时重算即可，结果逐位相同。存一份只会多制造
一个将来会漂移的第二来源（同一条纪律见 `app/style/features.py`）。
"""

from __future__ import annotations

from app.style import features
from app.style.schemas import AiStyleSignals


def ai_style_signals(text: str) -> AiStyleSignals:
    """算一条正文的确定性 AI 味辅助信号。纯函数，无 IO、无随机、无 LLM。"""
    text = text or ""
    return AiStyleSignals(
        char_count=len(text),
        template_opener_count=features.count_matches(text, features.TEMPLATE_OPENERS),
        empty_connective_count=features.count_matches(text, features.EMPTY_CONNECTIVES),
        false_authority_count=features.count_matches(text, features.FALSE_AUTHORITY),
        generic_phrase_count=len(features.find_generic_phrases(text)),
        hedge_count=features.count_hedges(text),
        repeated_opener_count=features.repeated_openers(features.split_sentences(text)),
        generic_phrases=features.find_generic_phrases(text),
    )


def format_signals(signals: AiStyleSignals) -> str:
    """渲染成一行给 Critic 看的说明。

    只印**非零**的项：全零时印"未发现"。把六个 0 都印出来会让提示里
    充满噪声，而噪声会让真正异常的那一条（比如"模板化开头 ×4"）
    淹没在里面——辅助信号的价值在于扎眼，不在于齐全。
    """
    parts: list[str] = []
    if signals.template_opener_count:
        parts.append(f"模板化开头 ×{signals.template_opener_count}")
    if signals.empty_connective_count:
        parts.append(f"空洞连接词 ×{signals.empty_connective_count}")
    if signals.false_authority_count:
        parts.append(f"虚假权威措辞 ×{signals.false_authority_count}")
    if signals.generic_phrase_count:
        hit = "、".join(signals.generic_phrases[:4])
        parts.append(f"套话 ×{signals.generic_phrase_count}（{hit}）")
    if signals.hedge_count:
        parts.append(f"模糊限定词 ×{signals.hedge_count}")
    if signals.repeated_opener_count:
        parts.append(f"重复句式开头 ×{signals.repeated_opener_count}")
    return "；".join(parts) if parts else "未发现"


__all__ = ["ai_style_signals", "format_signals"]
