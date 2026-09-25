"""LangGraph Checkpointer 的生命周期管理（PostgreSQL）。

与业务侧的 SQLAlchemy 连接（`app/db/session.py`）职责分开：
- 业务读写走 SQLAlchemy async engine / `app/repositories.py`；
- Graph 状态（checkpoint）走这里的 psycopg 连接池 + `AsyncPostgresSaver`。

两者 V1 指向同一个 PostgreSQL 数据库，但不共用连接。
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# State 里存的是 pydantic 模型。msgpack 默认只反序列化已注册的类型，未登记的类型
# 现在只是告警、未来版本会直接拒绝反序列化，所以显式登记。
#
# **这个清单必须覆盖 RunState 里的每一个类型。** 漏掉一个的后果不是"少个字段"，
# 而是恢复那个 Run 时整个 state 反序列化失败——而且只在真正走到那一步的运行上炸，
# 单元测试很可能碰不到。所以新增 Agent state 字段时，先来这里补名字。
SERDE = JsonPlusSerializer(
    allowed_msgpack_modules=[
        ("app.schemas", "ResearchResult"),
        # Phase 5A Agent Brain：见 app/agent/schemas.py
        ("app.agent.schemas", "AgentAction"),
        ("app.agent.schemas", "AgentDecision"),
        ("app.agent.schemas", "TopicCandidate"),
        ("app.agent.schemas", "TopicCandidateList"),
        ("app.agent.schemas", "EvidenceFact"),
        ("app.agent.schemas", "EvidencePack"),
        ("app.agent.schemas", "Angle"),
        ("app.agent.schemas", "AngleList"),
        ("app.agent.schemas", "Candidate"),
        ("app.agent.schemas", "CandidateList"),
        ("app.agent.schemas", "CandidateDraft"),
        ("app.agent.schemas", "CriticResult"),
    ]
)


def to_libpq_url(database_url: str) -> str:
    """把 SQLAlchemy URL 转成 libpq conninfo。

    `postgresql+psycopg://u:p@h:5432/db` → `postgresql://u:p@h:5432/db`

    psycopg 不认 SQLAlchemy 的 `+driver` 方言后缀，不能把 SQLAlchemy URL 原样
    传给 checkpointer，所以这里显式去掉方言部分。
    """
    scheme, sep, rest = database_url.partition("://")
    if not sep:
        return database_url
    return f"{scheme.split('+')[0]}://{rest}"


@asynccontextmanager
async def create_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """创建并初始化 checkpointer，退出时释放连接池。

    用连接池而非单连接：`AsyncPostgresSaver` 每完成一次 checkpoint 读/写都会从
    池里取一条连接，单连接在并发请求下会被多个协程同时使用。
    """
    pool = AsyncConnectionPool(
        conninfo=to_libpq_url(database_url),
        min_size=1,
        max_size=5,
        # 与官方 from_conn_string 的连接参数保持一致
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True, timeout=30.0)
    try:
        saver = AsyncPostgresSaver(pool, serde=SERDE)
        # 幂等：已建表则只跑迁移
        await saver.setup()
        yield saver
    finally:
        await pool.close()
