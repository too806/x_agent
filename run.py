"""本地启动入口：`python run.py`。

不要直接 `uvicorn app.main:app`：uvicorn 0.36+ 在 Windows 上会绕过 event loop
policy 选用 ProactorEventLoop，psycopg 的异步连接会全部失败（见 app/loops.py）。
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        loop="app.loops:selector_loop_factory",
    )
