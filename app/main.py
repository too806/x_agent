import logging
from dataclasses import dataclass

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
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
from app.services.llm import LLMError, OpenAILLMService
from app.services.x_api import RealXApiClient, XApiError
from app.services.x_service import XService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)

# 上游依赖（X API / LLM）失败 → 502 Bad Gateway：本服务本身正常，是外部提供方
# 返回了错误或不可用，调用方可按「依赖故障」处理（如重试）。
# 其余异常（代码缺陷、数据库错误等）→ 500 Internal Server Error。
_UPSTREAM_ERRORS = (XApiError, LLMError)


def _redact(message: str) -> str:
    """抹掉错误信息中可能出现的凭据，避免写入 DB 或返回给调用方。"""
    for secret in (
        settings.x_api_bearer_token,
        settings.llm_api_key,
        settings.x_api_key,
        settings.x_api_secret,
        settings.x_access_token,
    ):
        if secret:
            message = message.replace(secret, "***")
    return message


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
        error = _redact(str(exc)) or exc.__class__.__name__
        async with session_factory() as session:
            await mark_run_failed(session, run_id, error)
        status_code = 502 if isinstance(exc, _UPSTREAM_ERRORS) else 500
        # 失败时仍返回同一 RunResponse 结构（含 run_id / error），只把 HTTP 状态码
        # 改为非 2xx，使 HTTP 层与业务执行结果一致。
        return JSONResponse(
            status_code=status_code,
            content=RunResponse(run_id=run_id, status="failed", error=error).model_dump(),
        )

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
