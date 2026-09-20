import logging
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import GraphDeps, build_graph
from app.agent.persona import load_persona
from app.config import settings
from app.db.checkpointer import create_checkpointer
from app.db.session import async_session_factory
from app.repositories import (
    claim_run_for_review,
    create_agent_run,
    get_agent_run,
    get_draft_for_run,
    get_review_for_run,
    mark_run_completed,
    mark_run_failed,
    mark_run_waiting_for_review,
    release_run_review_claim,
)
from app.schemas import (
    REVIEW_ACTION_STATUS,
    ReviewDecision,
    ReviewPayload,
    ReviewRecord,
    RunDetailResponse,
    RunRequest,
    RunResponse,
)
from app.services.llm import LLMError, OpenAILLMService
from app.services.x_api import RealXApiClient, XApiError
from app.services.x_service import XService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


def _thread_config(run_id: str) -> dict:
    """LangGraph thread_id 恒等于 agent_run.id：invoke / interrupt / resume 全程一致。"""
    return {"configurable": {"thread_id": run_id}}


@dataclass
class Runtime:
    graph: object
    session_factory: async_sessionmaker[AsyncSession]


def build_runtime(checkpointer) -> Runtime:
    session_factory = async_session_factory
    deps = GraphDeps(
        x_service=XService(RealXApiClient()),
        llm_service=OpenAILLMService(),
        persona=load_persona(),
        session_factory=session_factory,
    )
    return Runtime(graph=build_graph(deps, checkpointer), session_factory=session_factory)


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    if _runtime is None:
        # checkpointer 必须与应用同生命周期，因此 Runtime 只在 lifespan 里构建：
        # 不在每个请求里创建/关闭数据库连接。
        raise RuntimeError("runtime is not initialized (application lifespan not started)")
    return _runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _runtime
    url = settings.langgraph_checkpoint_database_url or settings.database_url
    async with create_checkpointer(url) as checkpointer:
        _runtime = build_runtime(checkpointer)
        logger.info("runtime ready (checkpointer=postgres)")
        try:
            yield
        finally:
            _runtime = None
            logger.info("runtime closed")


app = FastAPI(title=settings.app_name, lifespan=lifespan)


def _describe_failure(run_id: str, exc: Exception) -> str:
    """记录失败日志（脱敏）并返回可对外返回的错误描述。"""
    # 显式格式化 traceback 并脱敏，而不是 logger.exception：日志同样不允许出现凭据
    logger.error(
        "run failed (run_id=%s, error=%s)%s", run_id, _redact(str(exc)), _redact(traceback.format_exc())
    )
    return _redact(str(exc)) or exc.__class__.__name__


def _failure_response(run_id: str, exc: Exception, *, status: str, error: str) -> JSONResponse:
    """失败时仍返回同一 RunResponse 结构（含 run_id / error），只把 HTTP 状态码
    改为非 2xx，使 HTTP 层与业务执行结果一致。"""
    # 上游依赖故障 → 502，其余（代码缺陷、数据库错误等）→ 500
    status_code = 502 if isinstance(exc, _UPSTREAM_ERRORS) else 500
    return JSONResponse(
        status_code=status_code,
        content=RunResponse(run_id=run_id, status=status, error=error).model_dump(),
    )


async def _fail_run(session_factory, run_id: str, exc: Exception) -> JSONResponse:
    """统一的失败收尾：落库 failed + 保留脱敏后的错误，返回非 2xx。"""
    error = _describe_failure(run_id, exc)
    async with session_factory() as session:
        await mark_run_failed(session, run_id, error)
    return _failure_response(run_id, exc, status="failed", error=error)


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
        final_state = await runtime.graph.ainvoke(
            {"run_id": run_id, "topic": request.topic}, config=_thread_config(run_id)
        )
    except Exception as exc:  # noqa: BLE001
        return await _fail_run(session_factory, run_id, exc)

    if "__interrupt__" in final_state:
        # 流程停在 human_review：Run 进入等待人工，HTTP 正常返回，不挂起等待
        async with session_factory() as session:
            await mark_run_waiting_for_review(session, run_id)
        payload = dict(final_state["__interrupt__"][0].value)
        logger.info("run waiting for review (run_id=%s)", run_id)
        return RunResponse(
            run_id=run_id,
            status="waiting_for_review",
            research_results=final_state.get("research_results", []),
            draft_content=final_state.get("draft_content"),
            draft_id=final_state.get("draft_id"),
            guardrail_passed=final_state.get("guardrail_passed"),
            guardrail_reasons=final_state.get("guardrail_reasons", []),
            review=ReviewPayload(**payload),
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
        guardrail_passed=final_state.get("guardrail_passed"),
        guardrail_reasons=final_state.get("guardrail_reasons", []),
    )


@app.get("/api/runs/{run_id}", response_model=RunDetailResponse)
async def get_run(
    run_id: uuid.UUID, runtime: Runtime = Depends(get_runtime)
) -> RunDetailResponse:
    run_id_str = str(run_id)
    session_factory = runtime.session_factory
    async with session_factory() as session:
        run = await get_agent_run(session, run_id_str)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        draft = await get_draft_for_run(session, run_id_str)
        review = await get_review_for_run(session, run_id_str)
        topic, status, error = run.topic, run.status, run.error
        draft_id = str(draft.id) if draft else None
        draft_content = draft.content if draft else None
        review_record = (
            ReviewRecord(
                review_id=str(review.id),
                action=review.status,
                original_content=review.original_content or "",
                edited_content=review.edited_content,
                reject_reason=review.reject_reason,
                reviewed_at=review.reviewed_at,
            )
            if review is not None
            else None
        )

    # Guardrail 结论与 final_content 只存在于 Graph state（checkpoint）里
    snapshot = await runtime.graph.aget_state(_thread_config(run_id_str))
    state = snapshot.values or {}

    return RunDetailResponse(
        run_id=run_id_str,
        topic=topic,
        status=status,
        draft_id=draft_id,
        draft_content=draft_content,
        guardrail_passed=state.get("guardrail_passed"),
        guardrail_reasons=state.get("guardrail_reasons", []),
        human_action=state.get("human_action"),
        final_content=state.get("final_content"),
        review=review_record,
        error=error,
    )


@app.post("/api/runs/{run_id}/review", response_model=RunResponse)
async def review_run(
    run_id: uuid.UUID,
    decision: ReviewDecision,
    runtime: Runtime = Depends(get_runtime),
) -> RunResponse:
    run_id_str = str(run_id)
    session_factory = runtime.session_factory

    async with session_factory() as session:
        run = await get_agent_run(session, run_id_str)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status != "waiting_for_review":
        raise HTTPException(
            status_code=409, detail=f"run is not waiting for review (status={run.status})"
        )

    final_status = REVIEW_ACTION_STATUS[decision.action]
    # 原子抢占：并发重复提交时只有一个请求能拿到这个 Run
    async with session_factory() as session:
        claimed = await claim_run_for_review(
            session, run_id_str, expected="waiting_for_review", new_status=final_status
        )
    if not claimed:
        raise HTTPException(status_code=409, detail="run review was already submitted")

    logger.info("review resume requested (run_id=%s, action=%s)", run_id_str, decision.action)
    try:
        # 同一个 run_id → 同一个 thread_id，从 checkpoint 恢复而不是新开线程
        final_state = await runtime.graph.ainvoke(
            Command(resume=decision.model_dump(exclude_none=True)),
            config=_thread_config(run_id_str),
        )
    except Exception as exc:  # noqa: BLE001
        # resume 失败：Graph 仍停在 human_review，Run 事实上还在等人工，把刚才抢到的
        # 状态放回去，同一个人工决定就能重试。否则一次瞬时的 checkpointer 故障会让
        # Run 永久不可恢复（checkpoint 完好，但状态已不是 waiting_for_review）。
        error = _describe_failure(run_id_str, exc)
        async with session_factory() as session:
            current = await release_run_review_claim(
                session, run_id_str, claimed_status=final_status, error=error
            )
        # current 为 None 只可能是 Run 行已不存在，此时无从判断状态，按 failed 返回
        return _failure_response(run_id_str, exc, status=current or "failed", error=error)

    logger.info("run %s (run_id=%s)", final_status, run_id_str)
    return RunResponse(
        run_id=run_id_str,
        status=final_status,
        draft_id=final_state.get("draft_id"),
        draft_content=final_state.get("draft_content"),
        guardrail_passed=final_state.get("guardrail_passed"),
        guardrail_reasons=final_state.get("guardrail_reasons", []),
        human_action=final_state.get("human_action"),
        final_content=final_state.get("final_content"),
    )
