from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agent.graph import GraphDeps, build_graph
from app.agent.persona import Persona
from app.config import settings
from app.main import Runtime, app, get_runtime
from app.models import AgentRun, Draft, ResearchItem
from app.services.llm import LLMError
from app.services.x_api import XApiError
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


def _runtime(session_factory, *, x_error=None, llm_error=None):
    deps = GraphDeps(
        x_service=XService(FakeXApiClient(error=x_error)),
        llm_service=FakeLLMService(text=draft_json(), error=llm_error),
        persona=PERSONA,
        session_factory=session_factory,
    )
    return Runtime(graph=build_graph(deps), session_factory=session_factory)


async def _post_run(session_factory, **runtime_kwargs):
    app.dependency_overrides[get_runtime] = lambda: _runtime(session_factory, **runtime_kwargs)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/runs", json={"topic": "AI coding agent"})
    finally:
        app.dependency_overrides.clear()


async def _runs(session_factory) -> list[AgentRun]:
    async with session_factory() as session:
        return list((await session.execute(select(AgentRun))).scalars().all())


async def test_create_run_endpoint(session_factory):
    resp = await _post_run(session_factory)

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


async def test_create_run_returns_non_2xx_when_x_api_fails(session_factory):
    resp = await _post_run(session_factory, x_error=XApiError("X API returned 401: unauthorized"))

    assert resp.status_code == 502
    data = resp.json()
    assert data["status"] == "failed"
    assert data["run_id"]
    assert "X API returned 401" in data["error"]

    runs = await _runs(session_factory)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error
    assert runs[0].finished_at is not None


async def test_create_run_returns_non_2xx_when_llm_fails(session_factory):
    resp = await _post_run(session_factory, llm_error=LLMError("LLM call failed: APIConnectionError"))

    assert resp.status_code == 502
    data = resp.json()
    assert data["status"] == "failed"
    assert data["run_id"]
    assert data["draft_id"] is None

    runs = await _runs(session_factory)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error
    assert runs[0].finished_at is not None


async def test_create_run_returns_500_on_unexpected_error(session_factory):
    resp = await _post_run(session_factory, x_error=RuntimeError("db exploded"))

    assert resp.status_code == 500
    assert resp.json()["status"] == "failed"
    assert (await _runs(session_factory))[0].status == "failed"


async def test_failure_response_does_not_leak_secrets(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "x_api_bearer_token", "super-secret-token")
    resp = await _post_run(
        session_factory,
        x_error=XApiError("X API returned 401: bearer super-secret-token rejected"),
    )

    assert resp.status_code == 502
    assert "super-secret-token" not in resp.text
    assert "***" in resp.json()["error"]

    runs = await _runs(session_factory)
    assert "super-secret-token" not in (runs[0].error or "")
