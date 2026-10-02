"""共享片段检测：一段文本是否搬运了另一段文本的原文（工作单第二十二、四十节）。

## 为什么在生产侧

这个检测器原本在 `evals/style/spans.py`，因为它的两个调用方都在评估侧
（`evals/style/selftest.py` 的合成基准、`scripts/smoke_5b2.py` 的真机验收）。
Phase 5B.3 多出第三个调用方——`app/agent/viewpoint.py` 用它判断一个观点是不是
只是把某条事实换了个说法——而 `app/` **不许** import `evals/`。

于是照 `app/style/features.py` 已经在文档里写下的同一条处置搬上来：代码放
`app/`，评估侧留一层薄适配器。理由与那次相同——**抄一份会在某天漂成两套口径，
而两套口径长得一样是最难发现的错误**。`evals/style/spans.py` 现在是转发。

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

## 纯函数契约

无 IO、无随机、不取挂钟时间、不读配置（同 `app/style/features.py`）。
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
