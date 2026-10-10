"""本地启动入口：`python run.py`。

不要直接 `uvicorn app.main:app`：uvicorn 0.36+ 在 Windows 上会绕过 event loop
policy 选用 ProactorEventLoop，psycopg 的异步连接会全部失败（见 app/loops.py）。
"""

import uvicorn

from app.logging_setup import configure_logging

if __name__ == "__main__":
    # 落盘在这里打开、**只在这里**：`app.main` 会被 pytest 直接 import，
    # 在那里开文件日志会让每次跑测试都往仓库的 logs/ 里写。
    # 位置必须在 uvicorn.run 之前——它在 import app.main 时就会打日志。
    log_path = configure_logging(to_file=True)
    print(f"backend log: {log_path}")

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        loop="app.loops:selector_loop_factory",
    )
