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
        ("app.agent.schemas", "CandidateTopic"),
        # Phase 5B.1 新增的枚举与 LLM 输出壳。枚举也必须登记：它们同样是
        # **按类名**反序列化的（`str` 混入只影响取值形态，不影响还原路径）。
        ("app.agent.schemas", "TopicLifecycle"),
        ("app.agent.schemas", "TopicQualification"),
        ("app.agent.schemas", "TopicClusterDraft"),
        ("app.agent.schemas", "TopicClusterDraftList"),
        # Phase 5A 的类名，现在指向 CandidateTopic（见 schemas.py 的说明）。
        # 登记一个**已经不再写出**的名字是刻意的：历史 checkpoint 是按类名还原的，
        # 去掉它会让那批 Run 的 candidate_topics 通道降级成 dict（LangGraph 对未登记
        # 类型只告警、不报错），而下游是按属性读的，读出来就是空标题。
        ("app.agent.schemas", "TopicCandidate"),
        # 注意：Phase 5A 的 `TopicCandidateList` **不在**这里。它只是 discover 的
        # 解析目标，从来没进过 RunState，所以没有 checkpoint 需要还原它；
        # 而它的类名在新代码里已经不存在了，登记一个解析不到的名字只会把
        # "少登记一个类型"换成"登记错一个名字"，两种都难查。
        ("app.agent.schemas", "EvidenceFact"),
        ("app.agent.schemas", "EvidencePack"),
        ("app.agent.schemas", "Angle"),
        ("app.agent.schemas", "AngleList"),
        ("app.agent.schemas", "Candidate"),
        ("app.agent.schemas", "CandidateList"),
        ("app.agent.schemas", "CandidateDraft"),
        ("app.agent.schemas", "CriticResult"),
        ("app.agent.schemas", "CriticReport"),
        ("app.agent.schemas", "DecisionTraceEntry"),
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
