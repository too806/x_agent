import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import GraphDeps, build_graph
from app.agent.persona import Persona, load_persona
from app.api.admin import create_admin_router
from app.config import settings
from app.db.checkpointer import create_checkpointer
from app.db.session import async_session_factory
from app.repositories import (
    create_agent_run,
    get_agent_run,
    get_draft_for_run,
    get_published_post,
    get_review_for_run,
    mark_run_completed,
    mark_run_failed,
    mark_run_waiting_for_review,
)
from app.schemas import (
    REVIEW_ACTION_STATUS,
    PublishRecord,
    PublishResolution,
    PublishResponse,
    ReviewDecision,
    ReviewPayload,
    ReviewRecord,
    RunDetailResponse,
    RunRequest,
    RunResponse,
    publish_record_from_row,
)
from app.services.llm import LLMError, OpenAILLMService
from app.services.publish import (
    OUTCOME_CONFLICT,
    OUTCOME_INVALID,
    OUTCOME_PUBLISHED,
    OUTCOME_RESOLVED,
    PublishResult,
    PublishService,
)
from app.services.review import (
    ResumeFailed,
    RunAlreadyClaimed,
    RunNotFound,
    RunNotWaitingForReview,
    describe_failure,
    resume_review,
)
from app.services.x_api import RealXApiClient, XApiError
from app.services.x_service import XService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 上游依赖（X API / LLM）失败 → 502 Bad Gateway：本服务本身正常，是外部提供方
# 返回了错误或不可用，调用方可按「依赖故障」处理（如重试）。
# 其余异常（代码缺陷、数据库错误等）→ 500 Internal Server Error。
_UPSTREAM_ERRORS = (XApiError, LLMError)


def _thread_config(run_id: str) -> dict:
    """LangGraph thread_id 恒等于 agent_run.id：invoke / interrupt / resume 全程一致。"""
    return {"configurable": {"thread_id": run_id}}


@dataclass
class Runtime:
    graph: object
    session_factory: async_sessionmaker[AsyncSession]
    publish_service: PublishService
    # persona 是**写作目标**的来源（Graph 的依赖里传一份），也留在 Runtime 上：
    # 控制台需要把"编辑稿写多长为宜"原样显示出来，而这个目标属于配置
    # （config/persona.yaml），不该在界面上再抄一份。
    #
    # 它**不再是发布门禁**：以前 PublishService 也拿一份 persona 去做超长 422，
    # 现在那道闸门撤了（见 app/services/publish.py 的 `_preflight`）。所以这里
    # 只是显示用，`content_max_length` 是提示不是保证。
    #
    # 默认值是为了让已有的构造点（含测试）无需改动；生产路径显式传入。
    persona: Persona = field(default_factory=load_persona)


def build_runtime(checkpointer) -> Runtime:
    session_factory = async_session_factory
    persona = load_persona()
    x_service = XService(RealXApiClient())
    deps = GraphDeps(
        x_service=x_service,
        llm_service=OpenAILLMService(),
        persona=persona,
        session_factory=session_factory,
    )
    graph = build_graph(deps, checkpointer)
    # 发布刻意不做成 Graph 节点：发生 interrupt() 的节点每次 resume 都会从头重放，
    # 把"发推文"这种不可幂等的动作放进一个设计上就会重放的执行器里是结构性风险。
    # 详见 app/services/publish.py 的模块说明。
    return Runtime(
        graph=graph,
        session_factory=session_factory,
        publish_service=PublishService(
            x_service=x_service,
            session_factory=session_factory,
            graph=graph,
        ),
        persona=persona,
    )


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

# 运营控制台（Phase 4b）：只读的 Admin API。挂载时把 get_runtime 作为依赖注入，
# 而不是让 app/api/admin.py 反过来 import main —— 避免循环导入，同时保住
# app.dependency_overrides[get_runtime] 在测试里的覆盖效果。
app.include_router(create_admin_router(get_runtime))


def _describe_failure(run_id: str, exc: Exception) -> str:
    """记录失败日志（脱敏）并返回可对外返回的错误描述。

    实现搬到了 `app/services/review.py`：自动发布那条路也要写失败原因到库里
    （`release_run_review_claim(error=...)`），两条路必须用同一份脱敏规则，
    否则总有一条会把 X 的 token 写进 DB。
    """
    return describe_failure(run_id, exc)


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
        run = await create_agent_run(session, request.topic, goal=request.goal)
    run_id = str(run.id)
    agent_mode = request.goal is not None
    logger.info(
        "run started (run_id=%s, mode=%s, topic=%r)",
        run_id,
        "agent" if agent_mode else "topic",
        request.goal if agent_mode else request.topic,
    )

    # 初始 State 只放属于这条链路的那个字段：图在 START 处按 `goal` 是否存在分岔，
    # 而给一条 topic 运行塞一个 goal=None 会让"判别式"多出一个恒假的字段，
    # 将来读代码的人要自己在脑子里把它排除掉。
    initial_state: dict[str, object] = {"run_id": run_id}
    if agent_mode:
        initial_state["goal"] = request.goal
    else:
        initial_state["topic"] = request.topic

    try:
        final_state = await runtime.graph.ainvoke(initial_state, config=_thread_config(run_id))
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
        published = await get_published_post(session, run_id_str)
        topic, status, error = run.topic, run.status, run.error
        finished_at = run.finished_at
        draft_id = str(draft.id) if draft else None
        draft_content = draft.content if draft else None
        publish_record = _publish_record(published)
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
        publish=publish_record,
        finished_at=finished_at,
        error=error,
    )


def _publish_record(row) -> PublishRecord | None:
    """published_post 行 → 对外形状。

    映射本身在 app/schemas.py，与运营控制台共用一份，避免两侧字段漂移。
    """
    if row is None:
        return None
    return publish_record_from_row(row)


async def _publish_payload(
    runtime: Runtime, run_id: str, result: PublishResult
) -> JSONResponse:
    """把发布结果映射成 HTTP。

    **刻意不走 `_failure_response` / `_fail_run`**：那两个会把 agent_run 打成
    failed 并返回 502。发布失败不是流水线失败——审核结论必须原样保留，
    发布的状态只记在 published_post 上。
    """
    async with runtime.session_factory() as session:
        row = await get_published_post(session, run_id)

    status_code = {
        OUTCOME_PUBLISHED: 200,
        OUTCOME_RESOLVED: 200,  # 人工裁决本身成功了；结果状态看 publish.status
        OUTCOME_CONFLICT: 409,
        OUTCOME_INVALID: 422,
    }.get(result.outcome, 502)  # failed / unknown 都是上游侧的问题 → 502

    body = PublishResponse(
        run_id=run_id,
        outcome=result.outcome,
        publish=_publish_record(row),
        error=result.detail,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def _audit_header(request: Request, name: str) -> str:
    """取一个请求头用于审计；缺失时给 `-`，日志才好 grep。

    顺带把值压成单行：请求头是客户端可控的，里面塞进换行就能伪造出额外的
    日志行（log injection），让事后阅读时误以为发生了别的事。
    """
    value = request.headers.get(name)
    if not value:
        return "-"
    return " ".join(value.split())


def _client_host(request: Request) -> str:
    """请求来源 `host:port`。`request.client` 不保证存在（测试的 ASGI
    transport、某些反代下会是 None），所以每一段都单独兜底，绝不能让它
    把一次发布打成 500。"""
    client = request.client
    if client is None:
        return "-"
    host = getattr(client, "host", None) or "-"
    port = getattr(client, "port", None)
    return f"{host}:{port}" if port is not None else host


@app.post("/api/runs/{run_id}/publish", response_model=PublishResponse)
async def publish_run(
    run_id: uuid.UUID,
    request: Request,
    runtime: Runtime = Depends(get_runtime),
) -> JSONResponse:
    run_id_str = str(run_id)
    async with runtime.session_factory() as session:
        run = await get_agent_run(session, run_id_str)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    # 这是**唯一会真的发出推文**的动作，所以这一行是整个系统里最值得审计的日志：
    # 事故复盘时要能回答"这条帖子是人从浏览器点的，还是脚本打的"。
    #
    # 取哪三样、以及**刻意不取什么**：
    #   - 只取 client_host / User-Agent / Referer，全部来自请求头
    #   - 不取 Cookie、不取 Authorization、不取任何 X API 凭据
    #   - **不取 request body**——发布请求体里就是 draft / edited_content 正文，
    #     一旦进日志就等于把待发内容抄进了一个长期保留的文件
    #
    # 它是**审计**信息，不是身份认证：本地服务没有鉴权，UA 与 Referer 都可以
    # 随便伪造。它的用途只有一个——把"机器发的"和"人点的"区分开。
    logger.info(
        "publish requested run_id=%s client_host=%s user_agent=%s referer=%s pid=%s",
        run_id_str,
        _client_host(request),
        _audit_header(request, "user-agent"),
        _audit_header(request, "referer"),
        os.getpid(),
    )
    # 不 try/except 包住：X 的各类错误在 PublishService 内部已经分类落库，
    # 这里再包一层只会把"发布失败"和"服务本身出错"混成同一件事。
    result = await runtime.publish_service.publish(run_id_str)
    return await _publish_payload(runtime, run_id_str, result)


@app.post("/api/runs/{run_id}/publish/resolve", response_model=PublishResponse)
async def resolve_publish(
    run_id: uuid.UUID,
    decision: PublishResolution,
    runtime: Runtime = Depends(get_runtime),
) -> JSONResponse:
    """人工裁决"结果未知"的发布。

    只纠正状态，不在这里发帖：重发必须另外显式再调一次 POST /publish。
    """
    run_id_str = str(run_id)
    async with runtime.session_factory() as session:
        run = await get_agent_run(session, run_id_str)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    result = await runtime.publish_service.resolve(
        run_id_str,
        action=decision.action,
        x_post_id=getattr(decision, "x_post_id", None),
    )
    logger.info("publish resolved (run_id=%s, action=%s)", run_id_str, decision.action)
    return await _publish_payload(runtime, run_id_str, result)


@app.post("/api/runs/{run_id}/review", response_model=RunResponse)
async def review_run(
    run_id: uuid.UUID,
    decision: ReviewDecision,
    runtime: Runtime = Depends(get_runtime),
) -> RunResponse:
    """人工评审：抢占 → 重放 Graph → 失败时归位。

    这三步连同失败处理都搬进了 `app/services/review.py::resume_review`，
    因为**无人值守的自动审阅者必须走同一条路**——否则自动侧会自己写一份 claim
    逻辑，两份迟早漂开，而漂开的表现是"偶尔有一条 run 谁都处理不了"。
    这里只剩下把 `ReviewError` 映射成 HTTP 码。

    `decision.model_dump(exclude_none=True)` 是**已校验**的决策模型，所以客户端
    塞进来的 `decided_by` 会被 pydantic 的 `extra="ignore"` 丢掉——HTTP 路径
    在结构上无法自称 auto，见 `app/agent/nodes/human_review.py::_decided_by_of`。
    """
    run_id_str = str(run_id)
    try:
        final_state = await resume_review(
            runtime,
            run_id_str,
            action=decision.action,
            resume_value=decision.model_dump(exclude_none=True),
        )
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (RunNotWaitingForReview, RunAlreadyClaimed) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResumeFailed as exc:
        # 失败时仍返回同一 RunResponse 结构（含 run_id / error），只把状态码改成
        # 非 2xx。`exc.released=False` 表示 checkpoint 其实已经推进过、状态没有
        # 被放回去，这种情况调用方**不要重试**——重试会撞上"状态说在等人、
        # review 行却已写好"的矛盾。见 services/review.py 的 ResumeFailed 说明。
        return _failure_response(run_id_str, exc.exc, status=exc.status, error=exc.error)

    final_status = REVIEW_ACTION_STATUS[decision.action]
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
