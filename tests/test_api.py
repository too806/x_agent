import httpx
from sqlalchemy import select

from app.config import settings
from app.models import AgentRun, Draft
from app.services.llm import LLMError
from app.services.x_api import RealXApiClient, XApiError
from app.services.x_service import XService
from fakes import FakeLLMService, FakeXApiClient, draft_json
from support import api_for, runtime_with_checkpointer


async def _runs(session_factory) -> list[AgentRun]:
    async with session_factory() as session:
        return list((await session.execute(select(AgentRun))).scalars().all())


async def _post_run(session_factory, **runtime_kwargs):
    async with runtime_with_checkpointer(session_factory, **runtime_kwargs) as runtime:
        async with api_for(runtime) as api:
            return await api.post("/api/runs", json={"topic": "AI coding agent"})


async def test_create_run_stops_waiting_for_review(session_factory):
    resp = await _post_run(session_factory)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "waiting_for_review"
    assert data["run_id"]
    assert len(data["research_results"]) == 2
    assert data["draft_id"]
    assert data["draft_content"]

    # Guardrail 结论随响应一起返回，供审核者参考
    assert data["guardrail_passed"] is True
    assert data["guardrail_reasons"] == []

    # interrupt payload 里的内容与响应一致
    assert data["review"]["type"] == "content_review"
    assert data["review"]["run_id"] == data["run_id"]
    assert data["review"]["draft_id"] == data["draft_id"]
    assert data["review"]["content"] == data["draft_content"]
    assert data["final_content"] is None
    assert data["error"] is None

    runs = await _runs(session_factory)
    assert len(runs) == 1
    assert runs[0].status == "waiting_for_review"
    assert runs[0].finished_at is None  # 流程尚未结束


async def test_create_run_returns_non_2xx_when_x_api_fails(session_factory):
    x_service = XService(FakeXApiClient(error=XApiError("X API returned 401: unauthorized")))
    resp = await _post_run(session_factory, x_service=x_service)

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


async def test_create_run_returns_502_when_x_api_returns_non_json(session_factory):
    # 端到端链路：RealXApiClient（非 JSON 响应体）→ XService → Research → HTTP 502
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>not json</html>")
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        x_service = XService(RealXApiClient(http_client=http_client))
        resp = await _post_run(session_factory, x_service=x_service)

    assert resp.status_code == 502
    assert "invalid JSON" in resp.json()["error"]
    assert (await _runs(session_factory))[0].status == "failed"


async def test_create_run_returns_non_2xx_when_llm_fails(session_factory):
    llm_service = FakeLLMService(
        text=draft_json(), error=LLMError("LLM call failed: APIConnectionError")
    )
    resp = await _post_run(session_factory, llm_service=llm_service)

    assert resp.status_code == 502
    data = resp.json()
    assert data["status"] == "failed"
    assert data["draft_id"] is None

    runs = await _runs(session_factory)
    assert runs[0].status == "failed"
    assert runs[0].error
    assert runs[0].finished_at is not None


async def test_create_run_returns_502_when_llm_returns_invalid_json(session_factory):
    resp = await _post_run(session_factory, llm_service=FakeLLMService(text="not-json"))

    assert resp.status_code == 502
    data = resp.json()
    assert data["status"] == "failed"
    assert "invalid draft structure" in data["error"]

    runs = await _runs(session_factory)
    assert runs[0].status == "failed"
    assert "invalid draft structure" in runs[0].error

    async with session_factory() as session:
        assert (await session.execute(select(Draft))).scalars().all() == []


async def test_create_run_returns_502_when_llm_violates_schema(session_factory):
    llm = FakeLLMService(text='{"content": "hi"}')  # 缺 hook_type / topic
    resp = await _post_run(session_factory, llm_service=llm)

    assert resp.status_code == 502
    assert resp.json()["status"] == "failed"
    assert (await _runs(session_factory))[0].status == "failed"


async def test_create_run_returns_500_on_unexpected_error(session_factory):
    x_service = XService(FakeXApiClient(error=RuntimeError("db exploded")))
    resp = await _post_run(session_factory, x_service=x_service)

    assert resp.status_code == 500
    assert resp.json()["status"] == "failed"
    assert (await _runs(session_factory))[0].status == "failed"


async def test_failure_response_does_not_leak_secrets(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "x_api_bearer_token", "super-secret-token")
    x_service = XService(
        FakeXApiClient(error=XApiError("X API returned 401: bearer super-secret-token rejected"))
    )
    resp = await _post_run(session_factory, x_service=x_service)

    assert resp.status_code == 502
    assert "super-secret-token" not in resp.text
    assert "***" in resp.json()["error"]

    runs = await _runs(session_factory)
    assert "super-secret-token" not in (runs[0].error or "")
