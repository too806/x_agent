"""共享片段检测：一段文本是否搬运了语料里的原文（工作单第二十二、四十节）。

## 为什么单独一个模块

这个检测器有**两个调用方**，而它们的语料不是同一份：

- `evals/style/selftest.py` —— 离线基准，语料是那 8 条合成样本；
- `scripts/smoke_5b2.py` —— 真机验收，语料是库里真实采到的样本。

两处各写一份实现的话，"离线基准通过"与"真实链路通过"就变成了两个不同的
命题，而它们本该是同一件事。检测口径必须只有一份，否则哪天有人调了阈值，
一边红一边绿，没人说得清哪边算数。

## 为什么是 n-gram 而不是"包含"

"整条原文被搬走"只是最笨的抄法，也是最先被发现的那种。真正会漏掉的是
**改了几个字的搬运**：换掉开头、补一句自己的话、把两段摘自不同样本的
半句接起来。这些都是 n-gram 能看见而子串包含看不见的。

## 阈值取 12

中文里 12 个连续字符几乎不可能在两个独立写成的句子里同时出现，而它又短到
够抓住"抄了半句"。调小会开始对常见搭配误报（"推理成本"这类词组），
调大会漏掉短样本被整条搬走的情形。

**先去掉空白再切片**：换行与空格是最容易制造表面差异的东西——
不归一化的话，把一条原文按每行拆开重排就能绕过检测。
"""

from __future__ import annotations

import re
from typing import Iterable

#: n-gram 长度。见模块说明里"为什么是 12"。
SHARED_SPAN = 12

_WHITESPACE = re.compile(r"\s+")


def spans_of(text: str, n: int = SHARED_SPAN) -> set[str]:
    """`text` 的 n-gram 集合（已去掉空白）。比 `n` 短时返回空集。

    返回集合而不是列表：调用方要的是"有没有共享的片段"，不是"共享了多少次"。
    同一条原文在正文里出现两次不该让检测结果看起来更严重。
    """
    stripped = _WHITESPACE.sub("", text or "")
    if len(stripped) < n:
        return set()
    return {stripped[index : index + n] for index in range(len(stripped) - n + 1)}


def corpus_spans(texts: Iterable[str], n: int = SHARED_SPAN) -> set[str]:
    """一批语料正文的全部 n-gram。"""
    spans: set[str] = set()
    for text in texts:
        spans |= spans_of(text, n)
    return spans


def overlapping_spans(
    text: str, spans: set[str], n: int = SHARED_SPAN
) -> list[str]:
    """`text` 里与语料共享的片段。空列表 = 没有搬运任何原文。

    参数收的是**已经算好的 span 集合**而不是语料本身：真实语料有几十上百条，
    逐条候选都重算一遍语料的 n-gram 会让一次 smoke 多出几百次无用计算。
    调用方算一次，这里复用。
    """
    return sorted(spans_of(text, n) & spans)


__all__ = ["SHARED_SPAN", "corpus_spans", "overlapping_spans", "spans_of"]
