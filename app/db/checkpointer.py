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

# State 里存的是 pydantic 模型。msgpack 只反序列化**已登记**的类型，所以显式登记。
#
# **登记规则（Phase 5B.2 实测澄清，与早先的注释不同）：**
#
# - 只登记**作为 state 顶层值**出现的类型。嵌在已登记模型的**类型化字段**里的
#   **模型**会被 `model_dump()` 内联，**不需要**登记——登记了就是永远用不到的死条目。
#   例：`CriticResult.style_fit` 是嵌套的 `StyleFit`，`StyleFit` **不登记**。
# - **枚举不适用上一条**：`model_dump()` 不会把它们拍成字符串，它们照样以
#   EXT 的形式出现在 payload 里。所以嵌套的枚举**要登记**（这也是 Phase 5B.1
#   登记 TopicLifecycle / TopicQualification 的原因——它们同样是嵌套的）。
# - 落在 `Any` 类型字段里的模型是独立的 EXT，**需要**登记。
#
# **为什么"没登记也能跑对"是一件危险的事**（Phase 5B.2 实测）：未登记的枚举
# 走 `_check_allowed` → 返回 False → 还原成**普通字符串**，日志里留一条 warning；
# 随后 pydantic 在类型化字段上**又把字符串校验回枚举**，于是最终对象是对的。
# 回归测试因此全绿，只有日志在响。这份"正确"完全寄生于 pydantic 的再校验：
# 哪天字段改成 `Any`、或模型换成 `model_construct`，同一段代码会静默拿到字符串，
# 而 `recipe.structure_type is StructureType.CONTRAST` 这类比较会**恒为 False**。
# 所以登记它们，并让 tests/test_style_writer_integration.py 里的往返断言
# 钉住"还原后仍是枚举"这个结果。
#
# **漏登记的后果不是"少个字段"，而是静默降级**：`_check_allowed` 返回 False 后
# 返回的是**原始 payload**——顶层模型退化成 `dict`、`list[Enum]` 退化成字符串列表，
# 只在日志里留一条 warning。下游按属性读，读出来就是空值，而且**没有任何报错**。
# 所以"补一个 isinstance 断言"比"补一个名字"更重要：
# 见 tests/test_agent_loop.py::test_agent_run_state_is_resumable_from_checkpoint。
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
        # Phase 5B.2：StyleRecipe 以**顶层值**进 RunState（state["style_recipe"]），
        # 所以必须登记。
        ("app.style.schemas", "StyleRecipe"),
        # 它内部引用的枚举——**嵌套的枚举照样要登记**（见上面的登记规则）。
        # 不登记不会立刻报错（pydantic 会把被拦下的字符串再校验回枚举），
        # 但每次读 checkpoint 都会刷七条 warning，而且正确性全靠那一步再校验。
        ("app.style.taxonomy", "StructureType"),
        ("app.style.taxonomy", "HookType"),
        ("app.style.schemas", "DensityLevel"),
        ("app.style.schemas", "StanceStrength"),
        ("app.style.schemas", "EvidenceRequirement"),
        ("app.style.schemas", "Tone"),
        ("app.style.schemas", "EndingType"),
        # 注意：`CriticResult.style_fit` 里的 `StyleFit` **不在**这里——它是嵌套的
        # **模型**，会被 model_dump() 内联，登记了就是用不到的死条目。
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
