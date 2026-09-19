import asyncio
import sys

# psycopg3 异步不支持 Windows 默认的 ProactorEventLoop，需统一用 SelectorEventLoop。
# 放在包 __init__ 中，保证应用(uvicorn)与 Alembic 迁移在 import app 时即生效。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
