"""Phase 3 测试共用工具：Runtime 构造（真实 PostgreSQL checkpointer）、API client、常量。"""

from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

from app.agent.graph import GraphDeps, build_graph
from app.agent.persona import Persona
from app.config import settings
from app.db.checkpointer import create_checkpointer, to_libpq_url
from app.main import Runtime, app, get_runtime
from app.services.publish import PublishService
from app.services.x_service import XService
from fakes import FakeLLMService, FakeXApiClient, draft_json

# 只换库名，主机/端口/凭据一律沿用 .env 的 DATABASE_URL —— 写死 localhost:5432
# 会让本机把 Postgres 挪到别的端口后，测试静默打到另一个实例上。
_DB_BASE = settings.database_url.rsplit("/", 1)[0]
ADMIN_SYNC = f"{_DB_BASE}/postgres"
TEST_URL = f"{_DB_BASE}/xagent_test"
CHECKPOINT_URL = to_libpq_url(TEST_URL)
_ALL_TABLES = "account, agent_run, research_item, draft, review, published_post, agent_decision"

PERSONA = Persona(
    name="default",
    description="简洁专业",
    tone="professional",
    style=["简洁"],
    avoid=["夸大"],
    max_length=280,
)


@asynccontextmanager
async def runtime_with_checkpointer(
    session_factory, *, x_service=None, llm_service=None, persona=PERSONA, budget=None
):
    """构建一个全新的 Runtime（自带独立 checkpointer 连接池）。

    每次进入都等价于「进程重建」：新的连接池 + 新编译的 Graph，没有任何
    进程内状态被复用，因此可以用于验证 checkpoint 恢复。

    `budget` 用于把 Agent 预算压小：验证"预算用尽会停下来"必须能在几步之内
    跑完，否则测试要么跑十几轮假 LLM，要么根本验不到终止条件。
    """
    async with create_checkpointer(CHECKPOINT_URL) as checkpointer:
        resolved_x_service = x_service or XService(FakeXApiClient())
        deps = GraphDeps(
            x_service=resolved_x_service,
            llm_service=llm_service or FakeLLMService(text=draft_json()),
            persona=persona,
            session_factory=session_factory,
        )
        graph = build_graph(deps, checkpointer, budget=budget)
        yield Runtime(
            graph=graph,
            session_factory=session_factory,
            publish_service=PublishService(
                x_service=resolved_x_service,
                session_factory=session_factory,
                graph=graph,
                persona=persona,
            ),
            # 与 PublishService 用同一个 persona 实例：控制台下发的
            # content_max_length 必须就是发布前校验会用的那个上限，否则测试里
            # 两者会各说各话（280 vs config/persona.yaml 的 140）。
            persona=persona,
        )


class FlakyGraph:
    """可开关的故障注入：arm() 之后的下一次 ainvoke 抛错，之后恢复正常。

    用来模拟 resume 期间的瞬时故障（PG 瞬断 / checkpointer 异常），而 Graph 本身
    与 checkpoint 都保持原样。
    """

    def __init__(self, graph):
        self._graph = graph
        self._armed = False

    def arm(self):
        self._armed = True

    async def ainvoke(self, *args, **kwargs):
        if self._armed:
            self._armed = False
            raise RuntimeError("simulated checkpointer outage")
        return await self._graph.ainvoke(*args, **kwargs)

    async def aget_state(self, *args, **kwargs):
        return await self._graph.aget_state(*args, **kwargs)


@asynccontextmanager
async def api_for(runtime):
    """把给定 Runtime 注入 FastAPI 依赖，产出一个 ASGI 测试 client。"""
    app.dependency_overrides[get_runtime] = lambda: runtime
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
