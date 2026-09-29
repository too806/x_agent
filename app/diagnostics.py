"""把异常渲染成一句**能用来排障**的话。

为什么值得单独一个模块：同一件事在本仓库里出现过三种写法——发布编排里挖
`__cause__`、HTTP 层只看 `str(exc)`、真机 smoke 脚本自己拼
`f"{type(exc).__name__}: {exc}"`。第三种写法在 2026-09-29 的 Phase 5B.1 冒烟里
给出了 `XApiTransportError: X API request could not be sent:` ——冒号后面什么都没有，
因为 httpx 连接失败时抛的 `ConnectError("")` 的 `str()` 就是空的，真正的原因
（DNS / 代理 / TLS）在 `__cause__` 里。一行读到尽头的错误信息等于没有信息。

所以收敛成一个函数，谁需要"描述这次失败"就用它。
"""

from app.redaction import redact

# 沿异常链往下挖的层数上限。链本身应该很浅，设上限只是为了防止意外的循环引用
# 把一条错误信息撑成一篇日志。
MAX_CAUSE_DEPTH = 5


def describe(exc: BaseException | None) -> str:
    """把异常连同它的原因链拼成可读诊断，再整体脱敏。

    只看 `str(exc)` 是不够的（见模块说明）。空消息退化成类名，保证至少能知道
    异常类型；链条走完仍然只有类名时，说明上游确实没提供更多信息，此时如实
    记录比编造更有用。

    `None` 返回空串：调用方通常直接把它写进数据库字段或报告，返回 `"None"`
    会让人以为真有一个叫 None 的异常。
    """
    if exc is None:
        return ""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < MAX_CAUSE_DEPTH:
        seen.add(id(current))
        text = str(current).strip() or type(current).__name__
        if text not in parts:
            parts.append(text)
        current = current.__cause__ or current.__context__
    return redact(" | ".join(parts))
