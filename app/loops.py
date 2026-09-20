"""uvicorn 的 event loop factory。

psycopg3 的异步驱动不支持 Windows 默认的 ProactorEventLoop。uvicorn 0.36+ 不再走
event loop policy，而是在 win32 且未使用 subprocess 时直接选用 ProactorEventLoop
（见 uvicorn.loops.asyncio.asyncio_loop_factory），因此必须在启动时显式指定
loop factory，否则应用启动阶段的数据库连接会失败：

    uvicorn app.main:app --loop app.loops:selector_loop_factory

等价入口：`python run.py`。
"""

import asyncio
import sys


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """返回 psycopg 异步驱动可用的 event loop。"""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
