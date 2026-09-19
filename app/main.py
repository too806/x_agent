import logging
from dataclasses import dataclass

from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import GraphDeps, build_graph
from app.agent.persona import load_persona
from app.config import settings
from app.db.session import async_session_factory
from app.repositories import (
    create_agent_run,
    mark_run_completed,
    mark_run_failed,
)
from app.schemas import RunRequest, RunResponse
from app.services.llm import OpenAILLMService
from app.services.x_api import RealXApiClient
from app.services.x_service import XService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)


@dataclass
class Runtime:
    graph: object
    session_factory: async_sessionmaker[AsyncSession]


def build_runtime() -> Runtime:
    session_factory = async_session_factory
    deps = GraphDeps(
        x_service=XService(RealXApiClient()),
        llm_service=OpenAILLMService(),
        persona=load_persona(),
        session_factory=session_factory,
    )
    return Runtime(graph=build_graph(deps), session_factory=session_factory)


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/runs", response_model=RunResponse)
async def create_run(request: RunRequest, runtime: Runtime = Depends(get_runtime)) -> RunResponse:
    session_factory = runtime.session_factory
    async with session_factory() as session:
        run = await create_agent_run(session, request.topic)
    run_id = str(run.id)
    logger.info("run started (run_id=%s, topic=%r)", run_id, request.topic)

    try:
        final_state = await runtime.graph.ainvoke({"run_id": run_id, "topic": request.topic})
    except Exception as exc:  # noqa: BLE001
        logger.exception("run failed (run_id=%s)", run_id)
        async with session_factory() as session:
            await mark_run_failed(session, run_id, str(exc))
        return RunResponse(run_id=run_id, status="failed", error=str(exc))

    async with session_factory() as session:
        await mark_run_completed(session, run_id)
    logger.info("run completed (run_id=%s)", run_id)

    return RunResponse(
        run_id=run_id,
        status="completed",
        research_results=final_state.get("research_results", []),
        draft_content=final_state.get("draft_content"),
        draft_id=final_state.get("draft_id"),
    )
