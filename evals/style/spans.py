"""转发层：检测器本体已上移到 `app/style/spans.py`。

## 为什么只剩转发

这个检测器本来在这里，因为它当时的两个调用方都在评估侧：
`evals/style/selftest.py`（合成基准）与 `scripts/smoke_5b2.py`（真机验收）。

Phase 5B.3 多出第三个调用方——`app/agent/viewpoint.py` 要用它判断一个观点
是不是只是把某条事实换了个说法——而仓库里只有一条允许的箭头 `evals → app`，
反过来是禁止的。于是照 `app/style/features.py` 记录过的同一条处置：代码搬上去，
这里留一层转发。

**不要再把函数抄回来**。抄一份就会在某天漂成两套口径，而两套口径长得一样
是最难发现的错误——这正是 `features.py` 当初上移的理由。

两个原调用方（`selftest.py` / `smoke_5b2.py`）的 import 路径保持不变。
"""

from app.style.spans import (  # noqa: F401
    SHARED_SPAN,
    corpus_spans,
    overlapping_spans,
    spans_of,
)

__all__ = ["SHARED_SPAN", "corpus_spans", "overlapping_spans", "spans_of"]
