import asyncio
import sys

# psycopg3 异步不支持 Windows 默认的 ProactorEventLoop，需统一用 SelectorEventLoop。
# 放在包 __init__ 中，保证 Alembic 迁移在 import app 时即生效。
# 注意：uvicorn 0.36+ 不再读取 event loop policy，启动服务时必须显式指定 loop
# factory，否则上面的设置不生效（见 app/loops.py 与 run.py）。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
