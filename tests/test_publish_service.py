"""发布编排的行为测试。X 全部为 fake，不产生任何外部副作用。

每个用例都在守一个具体的失败模式，尤其是"X 被调用了几次"——重复发推是本阶段
唯一不可挽回的错误，而它在只看返回值的断言下是完全隐形的。
"""
import asyncio

import httpx

from app.models.published_post import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_UNKNOWN,
)
from app.repositories import (
    REVIEW_STATUS_TO_RUN_STATUS,
    claim_run_for_review,
    create_agent_run,
    create_draft,
    create_review,
    get_agent_run,
    get_published_post,
)
from app.services.publish import (
    OUTCOME_CONFLICT,
    OUTCOME_FAILED,
    OUTCOME_INVALID,
    OUTCOME_PUBLISHED,
    OUTCOME_RESOLVED,
    OUTCOME_UNKNOWN,
    PublishService,
)
from app.services.x_api import (
    XApiConfigError,
    XApiError,
    XApiResponseError,
    XApiTransportError,
)
from fakes import FakePublishingXService, StubGraph
from support import PERSONA

CONTENT = "AI coding agents ship faster."
EDITED = "AI coding agents ship much faster."
POST_ID = "1900000000000000001"


async def _prepare(
    session_factory,
    *,
    draft_content: str = CONTENT,
    action: str = "approve",
    edited_content: str | None = None,
    run_status: str | None = None,
    final_content: str | None = None,
    with_review: bool = True,
) -> tuple[str, str]:
    """建好一个"已通过人工审核"的 Run，返回 (run_id, 预期的发布正文)。

    `run_status` 默认由 action 推导（approve→approved / edit→edited）：两者必须
    一致，否则发布会被交叉校验挡下——那正是我们要的，所以默认值不能各写各的。

    `with_review=False` 模拟"状态已经抢到手，但 resume 还没跑到写 review 行"的窗口。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)
        draft_id = await create_draft(
            session, run_id, draft_content, version=1, prompt_version="1", model_name="fake"
        )
    if with_review:
        async with session_factory() as session:
            await create_review(
                session,
                draft_id=draft_id,
                action=action,
                original_content=draft_content,
                edited_content=edited_content,
            )
    if run_status is None:
        run_status = REVIEW_STATUS_TO_RUN_STATUS[action]
    async with session_factory() as session:
        await claim_run_for_review(session, run_id, expected="running", new_status=run_status)

    if final_content is None:
        final_content = draft_content if action == "approve" else edited_content
    return run_id, final_content


async def _run_status(session_factory, run_id: str) -> str:
    async with session_factory() as session:
        run = await get_agent_run(session, run_id)
    return run.status


def _service(session_factory, x_service, final_content):
    return PublishService(
        x_service=x_service,
        session_factory=session_factory,
        graph=StubGraph({"final_content": final_content}),
        persona=PERSONA,
    )


async def _row(session_factory, run_id):
    async with session_factory() as session:
        return await get_published_post(session, run_id)


# ---------- 正常路径 ----------


async def test_publish_success_records_post_id(session_factory):
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(post_id=POST_ID)
    service = _service(session_factory, x, final_content)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_PUBLISHED
    assert result.x_post_id == POST_ID
    assert x.published == [CONTENT]  # 恰好一次，且正文正确

    row = await _row(session_factory, run_id)
    assert row.status == STATUS_PUBLISHED
    assert row.x_post_id == POST_ID
    assert row.published_at is not None
    assert row.last_error is None


async def test_edit_path_publishes_the_edited_content(session_factory):
    """edit 路径发布的必须是人工改写稿，不是原稿。"""
    run_id, final_content = await _prepare(
        session_factory, action="edit", edited_content=EDITED
    )
    x = FakePublishingXService()
    service = _service(session_factory, x, final_content)

    await service.publish(run_id)

    assert x.published == [EDITED]


async def test_publish_success_moves_run_to_published(session_factory):
    """成功时 Run 一并推进到 published 终态。

    Run 的状态在发布路径上只会在 approved/edited ↔ publishing/publish_unknown
    ↔ published 之间流转，**永远不会变成 failed**——那是流水线失败用的值。
    """
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(post_id=POST_ID)
    service = _service(session_factory, x, final_content)

    await service.publish(run_id)

    async with session_factory() as session:
        run = await get_agent_run(session, run_id)
    assert run.status == STATUS_PUBLISHED
    assert run.finished_at is not None  # 终态才记结束时刻


async def test_publish_failure_never_marks_the_run_failed(session_factory):
    """发布失败不是流水线失败：Run 必须原样回到审核结论。

    这条守的是"把已审核通过的 Run 打成 failed"那类事故——调用方会误判成
    审核失败而重试，而重试的正是发布。
    """
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(402, "402 credits depleted"))
    service = _service(session_factory, x, final_content)

    await service.publish(run_id)

    assert await _run_status(session_factory, run_id) == "approved"
    async with session_factory() as session:
        run = await get_agent_run(session, run_id)
    assert run.finished_at is None  # 还在等发布，不是终态
    assert run.error is None  # 发布错误不写进 agent_run.error


# ---------- 调用 X 之前就该拦下的（本地可判定）----------


async def test_missing_review_is_rejected_without_calling_x(session_factory):
    """状态已经抢到手，但 resume 还没写到 review 行 → 没有可信的正文。

    这是 `claim_run_for_review` 与 `create_review` 之间的真实窗口。此时**绝不能**
    回退去用 draft.content：那等于发布了没人批准过的内容。拒绝发布，等 resume
    跑完再发。
    """
    run_id, _ = await _prepare(session_factory, with_review=False)
    x = FakePublishingXService()
    service = _service(session_factory, x, None)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_CONFLICT
    assert x.published == []
    assert await _row(session_factory, run_id) is None


async def test_missing_checkpoint_content_still_publishes(session_factory):
    """checkpoint 里没有 final_content 不构成拒绝理由——review 才是事实来源。

    "checkpoint 还没写"正是上面那条窗口的正常表现，而 review 行已经落库了。
    把 checkpoint 当权威会让一个正常完成的审核无法发布。
    """
    run_id, _ = await _prepare(session_factory)
    x = FakePublishingXService()
    service = _service(session_factory, x, None)

    assert (await service.publish(run_id)).outcome == OUTCOME_PUBLISHED
    assert x.published == [CONTENT]


async def test_blank_checkpoint_content_is_rejected(session_factory):
    """checkpoint 有值就必须与审核记录一致；空串也是一次不一致。"""
    run_id, _ = await _prepare(session_factory)
    x = FakePublishingXService()
    service = _service(session_factory, x, "   ")

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_CONFLICT
    assert x.published == []


async def test_run_status_disagreeing_with_review_is_rejected(session_factory):
    """Run 说 edited、review 说 approve → 拒绝。

    这个不一致只可能来自一个窗口：状态已被抢到新值，而 resume 还没覆写**上一次
    残留**的 review 行。照那个过期决定发布，就会发出人这次并没有批准过的东西。
    """
    run_id, _ = await _prepare(
        session_factory, action="approve", run_status="edited", final_content=CONTENT
    )
    x = FakePublishingXService()
    service = _service(session_factory, x, CONTENT)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_CONFLICT
    assert x.published == []


async def test_content_mismatching_review_is_rejected(session_factory):
    """checkpoint 里的内容与审核记录不一致 → 拒绝发布。

    create_review 与 human_review 节点写的是同一份值，正常情况必然相等；
    不相等说明 checkpoint 与数据库分叉了，此时宁可拒绝。
    """
    run_id, _ = await _prepare(session_factory)
    x = FakePublishingXService()
    service = _service(session_factory, x, "something nobody approved")

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_CONFLICT
    assert x.published == []


async def test_rejected_content_cannot_be_published(session_factory):
    """reject 路径必须被挡住——这正是"发布了被拒绝的内容"那一类事故。"""
    run_id, _ = await _prepare(
        session_factory, action="reject", run_status="rejected", final_content=CONTENT
    )
    x = FakePublishingXService()
    service = _service(session_factory, x, CONTENT)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_CONFLICT
    assert x.published == []


async def test_run_not_approved_cannot_be_published(session_factory):
    run_id, final_content = await _prepare(
        session_factory, run_status="waiting_for_review"
    )
    x = FakePublishingXService()
    service = _service(session_factory, x, final_content)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_CONFLICT
    assert x.published == []


async def test_overlong_content_is_rejected_locally(session_factory):
    """超长由 persona.max_length 判定，不该浪费一次外部调用去换 X 的 400。"""
    long_content = "a" * (PERSONA.max_length + 1)
    run_id, final_content = await _prepare(
        session_factory, action="edit", edited_content=long_content
    )
    x = FakePublishingXService()
    service = _service(session_factory, x, final_content)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_INVALID
    assert str(PERSONA.max_length) in result.detail
    assert x.published == []
    assert await _row(session_factory, run_id) is None


async def test_content_at_max_length_is_allowed(session_factory):
    boundary = "a" * PERSONA.max_length
    run_id, final_content = await _prepare(
        session_factory, action="edit", edited_content=boundary
    )
    x = FakePublishingXService()
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_PUBLISHED
    assert x.published == [boundary]


# ---------- X 的答复分类 ----------


async def test_x_4xx_marks_failed_and_is_retryable(session_factory):
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(402, "402 credits depleted"))
    service = _service(session_factory, x, final_content)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_FAILED
    assert result.status == STATUS_FAILED
    row = await _row(session_factory, run_id)
    assert row.status == STATUS_FAILED
    assert "402" in row.last_error
    assert row.x_post_id is None


async def test_x_5xx_ends_in_publish_unknown(session_factory):
    """5xx 不能判 failed：X 可能已经创建了内容再报错。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(503, "503 unavailable"))
    service = _service(session_factory, x, final_content)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_UNKNOWN
    row = await _row(session_factory, run_id)
    assert row.status == STATUS_UNKNOWN
    assert "503" in row.last_error
    # Run 也停在"结果未知"，而不是回到可发布状态——回去就等于允许自动重发
    assert await _run_status(session_factory, run_id) == STATUS_UNKNOWN


async def test_read_timeout_ends_in_publish_unknown(session_factory):
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(
        error=XApiTransportError("read timed out", may_have_reached_x=True)
    )
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_UNKNOWN
    assert (await _row(session_factory, run_id)).status == STATUS_UNKNOWN


async def test_connect_failure_marks_failed(session_factory):
    """连接都没建立起来 → 确定没发出去，可以重试。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(
        error=XApiTransportError("connect refused", may_have_reached_x=False)
    )
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_FAILED
    assert (await _row(session_factory, run_id)).status == STATUS_FAILED


async def test_missing_credentials_mark_failed_not_unknown(session_factory):
    """凭据缺失在任何 HTTP 请求之前抛出 → 确定没发出去。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiConfigError("missing X_ACCESS_TOKEN_SECRET"))
    service = _service(session_factory, x, final_content)

    result = await service.publish(run_id)

    assert result.outcome == OUTCOME_FAILED
    assert (await _row(session_factory, run_id)).status == STATUS_FAILED


async def test_unknown_x_error_defaults_to_unknown(session_factory):
    """兜底方向必须是"结果未知"，绝不能是"确定没发出去"。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiError("something new and unrecognised"))
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_UNKNOWN


async def test_errors_are_redacted_before_storage(session_factory, monkeypatch):
    """写进库的错误信息不能带凭据。"""
    from app.config import settings

    monkeypatch.setattr(settings, "x_access_token_secret", "SUPER-SECRET-VALUE", raising=False)
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(
        error=XApiResponseError(400, "rejected token SUPER-SECRET-VALUE")
    )
    service = _service(session_factory, x, final_content)

    await service.publish(run_id)

    row = await _row(session_factory, run_id)
    assert "SUPER-SECRET-VALUE" not in row.last_error
    assert "***" in row.last_error


# ---------- 诊断质量：写进库的信息必须能用来排障 ----------


def _transport_error_as_httpx_raises_it() -> XApiTransportError:
    """还原 2026-09-23 实机遇到的形状。

    httpx 连接失败时抛的是 `ConnectError("")`——`str()` 是空的，真正的原因
    （DNS / 代理 / TLS）在 `__cause__` 里；x_api 再包一层，于是外层消息变成
    `"X API request could not be sent: "`，冒号后什么都没有。这里如实重建这条
    异常链，而不是捏造一个带消息的异常，否则测试就绕开了要修的 bug。
    """
    try:
        try:
            raise httpx.ConnectError("") from OSError("All connection attempts failed")
        except httpx.ConnectError as exc:
            raise XApiTransportError(
                f"X API request could not be sent: {exc}", may_have_reached_x=False
            ) from exc
    except XApiTransportError as exc:
        return exc


async def test_uninformative_transport_error_reports_its_cause(session_factory):
    """空消息的传输错误必须把底层原因带出来，否则排障时无迹可寻。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=_transport_error_as_httpx_raises_it())
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_FAILED

    error = (await _row(session_factory, run_id)).last_error
    # 底层原因（凭据之外的网络信息）必须可见
    assert "All connection attempts failed" in error
    # 异常类型也要保留：链条断在某一层时至少知道断在哪
    assert "ConnectError" in error


async def test_cause_chain_is_redacted_too(session_factory, monkeypatch):
    """原因链是新引入的信息来源，凭据脱敏必须跟着一起往下走。"""
    from app.config import settings

    monkeypatch.setattr(settings, "x_access_token_secret", "SUPER-SECRET-VALUE", raising=False)
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(
        error=XApiTransportError(
            "X API request could not be sent: ", may_have_reached_x=False
        )
    )
    x._error.__cause__ = OSError("proxy auth failed with SUPER-SECRET-VALUE")
    service = _service(session_factory, x, final_content)

    await service.publish(run_id)

    error = (await _row(session_factory, run_id)).last_error
    assert "SUPER-SECRET-VALUE" not in error
    assert "***" in error


async def test_cause_chain_walk_is_bounded(session_factory):
    """异常链可能自我循环，诊断不能因此变成死循环。"""
    run_id, final_content = await _prepare(session_factory)
    error = XApiTransportError("connect refused", may_have_reached_x=False)
    # 制造一个自引用链：__context__ 指回自己
    error.__context__ = error
    x = FakePublishingXService(error=error)
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_FAILED
    assert (await _row(session_factory, run_id)).last_error.count("connect refused") == 1


# ---------- 幂等：本阶段唯一不可挽回的错误 ----------


async def test_second_publish_does_not_call_x_again(session_factory):
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(post_id=POST_ID)
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_PUBLISHED
    second = await service.publish(run_id)

    assert second.outcome == OUTCOME_CONFLICT
    assert second.x_post_id == POST_ID  # 冲突结果里带回已有记录
    assert len(x.published) == 1  # 关键断言：X 只被调用过一次


async def test_retry_after_failure_calls_x_again_exactly_once(session_factory):
    """失败后的重试必须真的能再发一次——且只再发一次。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(402, "402 credits depleted"))
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_FAILED

    x._error = None  # noqa: SLF001 - 模拟额度恢复
    assert (await service.publish(run_id)).outcome == OUTCOME_PUBLISHED
    assert len(x.published) == 2  # 两次尝试各一次
    assert (await _row(session_factory, run_id)).status == STATUS_PUBLISHED


async def test_unknown_attempt_is_never_retried_automatically(session_factory):
    """结果未知的行不能被再次抢占——抢占它就是重复发推。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(503, "503 unavailable"))
    service = _service(session_factory, x, final_content)

    assert (await service.publish(run_id)).outcome == OUTCOME_UNKNOWN
    assert (await service.publish(run_id)).outcome == OUTCOME_CONFLICT

    assert len(x.published) == 1  # 只发过一次


async def test_concurrent_publish_calls_x_exactly_once(session_factory):
    """并发发布：三个请求同时到达，X 只能被调用一次。

    这是整个阶段最重要的一条断言。只看返回值的测试在这里完全无效——
    没有抢占保护时三个请求都返回 200，X 却被调用了三次。
    """
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(post_id=POST_ID)
    service = _service(session_factory, x, final_content)

    results = await asyncio.gather(*[service.publish(run_id) for _ in range(3)])

    assert len(x.published) == 1, [r.outcome for r in results]
    assert [r.outcome for r in results].count(OUTCOME_PUBLISHED) == 1
    assert (await _row(session_factory, run_id)).status == STATUS_PUBLISHED


async def test_concurrent_retry_calls_x_exactly_once(session_factory):
    """并发重试同样只能有一个赢家。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(402, "402 credits depleted"))
    service = _service(session_factory, x, final_content)
    await service.publish(run_id)

    x._error = None  # noqa: SLF001
    results = await asyncio.gather(*[service.publish(run_id) for _ in range(3)])

    assert [r.outcome for r in results].count(OUTCOME_PUBLISHED) == 1
    assert len(x.published) == 2  # 首次失败那次 + 重试成功一次


# ---------- 人工裁决 ----------


async def test_resolve_mark_published(session_factory):
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(503, "503 unavailable"))
    service = _service(session_factory, x, final_content)
    await service.publish(run_id)

    result = await service.resolve(run_id, action="mark_published", x_post_id=POST_ID)

    # outcome 表达"裁决生效了"，结果状态在 status 里
    assert result.outcome == OUTCOME_RESOLVED
    assert result.status == STATUS_PUBLISHED
    row = await _row(session_factory, run_id)
    assert row.status == STATUS_PUBLISHED
    assert row.x_post_id == POST_ID
    assert len(x.published) == 1  # 裁决本身不发帖


async def test_resolve_retry_only_flips_status(session_factory):
    """裁决 retry 只纠正状态，不发帖——重发必须另外显式调一次 /publish。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(503, "503 unavailable"))
    service = _service(session_factory, x, final_content)
    await service.publish(run_id)

    result = await service.resolve(run_id, action="retry")

    assert result.outcome == OUTCOME_RESOLVED
    assert result.status == STATUS_FAILED
    row = await _row(session_factory, run_id)
    assert row.status == STATUS_FAILED
    assert len(x.published) == 1  # 裁决没有触发任何发布

    # 显式重发才真的再调一次
    x._error = None  # noqa: SLF001 - 模拟"人工确认没发出去，且 X 已恢复"
    assert (await service.publish(run_id)).outcome == OUTCOME_PUBLISHED
    assert len(x.published) == 2


async def test_resolve_on_published_row_is_rejected(session_factory):
    """已经确认发布成功的行不允许再裁决。"""
    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(post_id=POST_ID)
    service = _service(session_factory, x, final_content)
    await service.publish(run_id)

    result = await service.resolve(run_id, action="retry")

    assert result.outcome == OUTCOME_CONFLICT
    assert (await _row(session_factory, run_id)).status == STATUS_PUBLISHED


async def test_published_is_not_a_dedup_basis_before_it_actually_went_out(session_factory):
    """发布成功之后，正文才会进入 Guardrail 的判重来源。"""
    from app.repositories import list_published_contents

    run_id, final_content = await _prepare(session_factory)
    x = FakePublishingXService(error=XApiResponseError(503, "503 unavailable"))
    service = _service(session_factory, x, final_content)

    await service.publish(run_id)
    async with session_factory() as session:
        assert await list_published_contents(session) == []

    await service.resolve(run_id, action="mark_published", x_post_id=POST_ID)
    async with session_factory() as session:
        assert await list_published_contents(session) == [CONTENT]
