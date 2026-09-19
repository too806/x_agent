from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agent.graph import GraphDeps, build_graph
from app.agent.persona import Persona
from app.main import Runtime, app, get_runtime
from app.models import AgentRun, Draft, ResearchItem
from app.services.x_service import XService
from fakes import FakeLLMService, FakeXApiClient, draft_json

PERSONA = Persona(
    name="default",
    description="简洁专业",
    tone="professional",
    style=["简洁"],
    avoid=["夸大"],
    max_length=280,
)


def _runtime(session_factory):
    deps = GraphDeps(
        x_service=XService(FakeXApiClient()),
        llm_service=FakeLLMService(text=draft_json()),
        persona=PERSONA,
        session_factory=session_factory,
    )
    return Runtime(graph=build_graph(deps), session_factory=session_factory)


async def test_create_run_endpoint(session_factory):
    app.dependency_overrides[get_runtime] = lambda: _runtime(session_factory)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/runs", json={"topic": "AI coding agent"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["run_id"]
        assert len(data["research_results"]) == 2
        assert data["draft_content"]

        async with session_factory() as session:
            runs = (await session.execute(select(AgentRun))).scalars().all()
            items = (await session.execute(select(ResearchItem))).scalars().all()
            drafts = (await session.execute(select(Draft))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == "completed"
            assert len(items) == 2
            assert len(drafts) == 1
    finally:
        app.dependency_overrides.clear()
