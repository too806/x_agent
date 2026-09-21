"""发布状态机的仓储层测试。

这一层的每一条断言都在守一个具体的失败模式，不是"跑通就行"：
并发抢占、失败后重试可达性、以及"证据压过猜测"的不对称写入规则。
X 完全不出现在这里——本文件只验证数据库层面的状态转移。

发布状态分散在两张表上（agent_run.status 与 published_post.status），因此
**每条断言都要同时看两边**：只查一边的测试在"两表分叉"这类 bug 面前是隐形的。
"""
import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import AgentRun, PublishedPost
from app.models.published_post import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_UNKNOWN,
)
from app.repositories import (
    REVIEW_STATUS_TO_RUN_STATUS,
    claim_publish,
    claim_run_for_review,
    confirm_published_manually,
    create_agent_run,
    create_draft,
    create_review,
    get_published_post,
    list_published_contents,
    mark_publish_failed,
    mark_published,
    note_publish_unknown,
    reopen_publish_for_retry,
)

CONTENT = "AI coding agents ship faster."
POST_ID = "1900000000000000001"


async def _make_run_and_draft(session_factory, *, action: str = "approve") -> tuple[str, str]:
    """建一个「已通过人工审核、可以发布」的 Run。

    claim_publish 现在要 CAS agent_run 的状态，所以测试必须先把 Run 放到
    approved / edited —— 这本身就是"只有审核通过的内容才能发布"的体现。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)
        draft_id = await create_draft(
            session, run_id, CONTENT, version=1, prompt_version="1", model_name="fake"
        )
    async with session_factory() as session:
        await create_review(session, draft_id=draft_id, action=action, original_content=CONTENT)
    async with session_factory() as session:
        await claim_run_for_review(
            session,
            run_id,
            expected="running",
            new_status=REVIEW_STATUS_TO_RUN_STATUS[action],
        )
    return run_id, draft_id


async def _claim(session_factory, run_id: str, draft_id: str) -> bool:
    async with session_factory() as session:
        return await claim_publish(
            session, agent_run_id=run_id, draft_id=draft_id, body=CONTENT
        )


async def _status(session_factory, run_id: str) -> str | None:
    async with session_factory() as session:
        row = await get_published_post(session, run_id)
        return row.status if row else None


async def _run_status(session_factory, run_id: str) -> str:
    async with session_factory() as session:
        run = await session.get(AgentRun, uuid.UUID(run_id))
        return run.status


async def test_claim_creates_publishing_row(session_factory):
    run_id, draft_id = await _make_run_and_draft(session_factory)

    assert await _claim(session_factory, run_id, draft_id) is True

    async with session_factory() as session:
        row = await get_published_post(session, run_id)
        assert row.status == STATUS_PUBLISHING
        assert row.body == CONTENT
        assert row.x_post_id is None
        assert row.published_at is None
        assert row.last_error is None
    # Run 与发布行必须在同一个事务里一起进入 publishing：
    # 只有一边动过，就意味着崩溃恢复时两张表会互相矛盾。
    assert await _run_status(session_factory, run_id) == STATUS_PUBLISHING


async def test_claim_requires_publishable_run(session_factory):
    """还没审核通过的 Run 不能被抢占——这是"发布了没人批准的内容"的第一道闸。"""
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)
        draft_id = await create_draft(
            session, run_id, CONTENT, version=1, prompt_version="1", model_name="fake"
        )

    # status 仍是 running
    assert await _claim(session_factory, run_id, draft_id) is False
    assert await _status(session_factory, run_id) is None
    assert await _run_status(session_factory, run_id) == "running"


async def test_second_claim_while_publishing_is_rejected(session_factory):
    """结果未知的行不可被再次抢占——抢占它就是重复发推。"""
    run_id, draft_id = await _make_run_and_draft(session_factory)

    assert await _claim(session_factory, run_id, draft_id) is True
    assert await _claim(session_factory, run_id, draft_id) is False


async def test_published_cannot_be_reclaimed(session_factory):
    run_id, draft_id = await _make_run_and_draft(session_factory)

    assert await _claim(session_factory, run_id, draft_id) is True
    async with session_factory() as session:
        assert await mark_published(session, agent_run_id=run_id, x_post_id=POST_ID) is True

    assert await _claim(session_factory, run_id, draft_id) is False
    assert await _status(session_factory, run_id) == STATUS_PUBLISHED
    assert await _run_status(session_factory, run_id) == STATUS_PUBLISHED


async def test_failed_attempt_can_be_reclaimed(session_factory):
    """回归保护：失败后的重试必须可达。

    早先的设计用 INSERT 建行，重试时撞 UNIQUE(agent_run_id) → 409 永久卡死，
    一次失败就再也不能发布。现在必须走条件 UPDATE 复用同一行。

    同时验证 Run 回退到了审核结论：**发布失败不写 failed**。
    """
    run_id, draft_id = await _make_run_and_draft(session_factory)

    assert await _claim(session_factory, run_id, draft_id) is True
    async with session_factory() as session:
        assert await mark_publish_failed(session, agent_run_id=run_id, error="400 bad request")
    assert await _status(session_factory, run_id) == STATUS_FAILED
    assert await _run_status(session_factory, run_id) == "approved"

    assert await _claim(session_factory, run_id, draft_id) is True
    assert await _status(session_factory, run_id) == STATUS_PUBLISHING
    assert await _run_status(session_factory, run_id) == STATUS_PUBLISHING


async def test_retry_reuses_the_same_row(session_factory):
    """重试不能产生第二行——否则"一个 Run 一条发布记录"的约束形同虚设。"""
    run_id, draft_id = await _make_run_and_draft(session_factory)

    await _claim(session_factory, run_id, draft_id)
    async with session_factory() as session:
        await mark_publish_failed(session, agent_run_id=run_id, error="boom")
    await _claim(session_factory, run_id, draft_id)

    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(PublishedPost))
    assert count == 1


async def test_retry_clears_stale_error(session_factory):
    run_id, draft_id = await _make_run_and_draft(session_factory)

    await _claim(session_factory, run_id, draft_id)
    async with session_factory() as session:
        await mark_publish_failed(session, agent_run_id=run_id, error="400 bad request")
    await _claim(session_factory, run_id, draft_id)

    async with session_factory() as session:
        row = await get_published_post(session, run_id)
    assert row.last_error is None


async def test_concurrent_claims_exactly_one_wins(session_factory):
    """并发抢占：三个请求只有一个能拿到，其余必须被拒。

    串行化点是 **agent_run 的条件 UPDATE** —— 三个请求改同一行，PostgreSQL 行锁
    让后来者排队，等前者提交后重检 WHERE 拿到 0 行。只断言"返回 200"是不够的：
    这里断言的是**获胜者恰好一个**，以及发布行恰好一条。
    """
    run_id, draft_id = await _make_run_and_draft(session_factory)

    results = await asyncio.gather(
        *[_claim(session_factory, run_id, draft_id) for _ in range(3)],
        return_exceptions=True,
    )

    assert [r for r in results if isinstance(r, Exception)] == []
    assert results.count(True) == 1, results
    assert await _status(session_factory, run_id) == STATUS_PUBLISHING
    assert await _run_status(session_factory, run_id) == STATUS_PUBLISHING
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(PublishedPost))
    assert count == 1


async def test_one_record_per_run_is_enforced_by_the_schema(session_factory):
    """UNIQUE(agent_run_id) 守的是 schema 级不变量，不是并发。

    采纳 agent_run 层 CAS 之后，并发已经由行锁串行化，唯一约束退回第二道防线。
    它的价值因此必须**直接**验证：绕过 claim_publish 手工插第二行，schema 必须
    拒绝。否则将来有人误删这条约束，并发测试照样全绿。
    """
    run_id, draft_id = await _make_run_and_draft(session_factory)
    assert await _claim(session_factory, run_id, draft_id) is True

    async with session_factory() as session:
        session.add(
            PublishedPost(
                agent_run_id=uuid.UUID(run_id),
                draft_id=uuid.UUID(draft_id),
                body=CONTENT,
                status=STATUS_PUBLISHING,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_concurrent_retries_exactly_one_wins(session_factory):
    """失败行上的并发重试同样只能有一个赢家：否则两个请求都以为自己是本次尝试。"""
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)
    async with session_factory() as session:
        await mark_publish_failed(session, agent_run_id=run_id, error="400 bad request")

    results = await asyncio.gather(
        *[_claim(session_factory, run_id, draft_id) for _ in range(3)],
        return_exceptions=True,
    )

    assert [r for r in results if isinstance(r, Exception)] == []
    assert results.count(True) == 1, results


async def test_published_overrides_human_resolve_during_flight(session_factory):
    """证据压过猜测：人工在我们飞行途中把 publishing 判成 failed，X 的 2xx 仍须纠正。

    场景：请求 A 正在调 X；人工抢先把状态 resolve 成 failed（他看到的 X 页面上还没有）。
    随后 A 拿到 2xx。若此时 mark_published 只认 publishing，post_id 就丢了，
    库里记着 failed → 下次重发 = 重复推文（正是本设计要防的事故）。
    """
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)

    async with session_factory() as session:
        assert await reopen_publish_for_retry(session, agent_run_id=run_id) is True
    assert await _status(session_factory, run_id) == STATUS_FAILED

    async with session_factory() as session:
        assert await mark_published(session, agent_run_id=run_id, x_post_id=POST_ID) is True

    async with session_factory() as session:
        row = await get_published_post(session, run_id)
    assert row.status == STATUS_PUBLISHED
    assert row.x_post_id == POST_ID
    # 猜测压不过事实，Run 也一样：人工刚判过的 approved 必须被改回 published
    assert await _run_status(session_factory, run_id) == STATUS_PUBLISHED


async def test_failure_does_not_clobber_published(session_factory):
    """猜测不能覆盖事实：迟到的 4xx 不允许把已确认的成功改写成 failed。"""
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)
    async with session_factory() as session:
        await mark_published(session, agent_run_id=run_id, x_post_id=POST_ID)

    async with session_factory() as session:
        assert await mark_publish_failed(session, agent_run_id=run_id, error="late 400") is False

    async with session_factory() as session:
        row = await get_published_post(session, run_id)
    assert row.status == STATUS_PUBLISHED
    assert row.x_post_id == POST_ID


async def test_unknown_ends_in_publish_unknown_and_records_reason(session_factory):
    """超时 / 5xx：尝试已经结束但结果无法判定 → publish_unknown，只留一行说明。

    刻意**不是** publishing：publishing 表示"本地对结果一无所知（可能还在发）"，
    两种都进人工裁决、都不自动重试，但运维含义不同——区分开才知道该不该再等。
    """
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)

    async with session_factory() as session:
        await note_publish_unknown(session, agent_run_id=run_id, error="timeout after 30s")

    async with session_factory() as session:
        row = await get_published_post(session, run_id)
    assert row.status == STATUS_UNKNOWN
    assert row.x_post_id is None
    assert row.last_error == "timeout after 30s"
    assert await _run_status(session_factory, run_id) == STATUS_UNKNOWN


async def test_unknown_row_can_still_be_reviewed_manually(session_factory):
    """publish_unknown 与 publishing 一样必须能被人工裁决——否则 Run 永久卡死。"""
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)
    async with session_factory() as session:
        await note_publish_unknown(session, agent_run_id=run_id, error="timeout")

    async with session_factory() as session:
        assert await reopen_publish_for_retry(session, agent_run_id=run_id) is True

    assert await _status(session_factory, run_id) == STATUS_FAILED
    assert await _run_status(session_factory, run_id) == "approved"
    # 裁决之后必须真的能重发
    assert await _claim(session_factory, run_id, draft_id) is True


async def test_manual_confirm_requires_publishing(session_factory):
    """人工确认只能作用于结果未知的行。

    对 failed 行调用必须无效：failed 的语义是"确定没发出去"，
    把它改成"已发布"是自相矛盾的状态，不该被接口允许。
    """
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)
    async with session_factory() as session:
        await mark_publish_failed(session, agent_run_id=run_id, error="400 bad request")

    async with session_factory() as session:
        assert (
            await confirm_published_manually(session, agent_run_id=run_id, x_post_id=POST_ID)
            is False
        )

    assert await _status(session_factory, run_id) == STATUS_FAILED


async def test_manual_confirm_marks_published(session_factory):
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)

    async with session_factory() as session:
        assert (
            await confirm_published_manually(session, agent_run_id=run_id, x_post_id=POST_ID)
            is True
        )

    async with session_factory() as session:
        row = await get_published_post(session, run_id)
    assert row.status == STATUS_PUBLISHED
    assert row.x_post_id == POST_ID
    assert row.published_at is not None
    # 裁决把 Run 一并推进到终态，并记上 finished_at
    assert await _run_status(session_factory, run_id) == STATUS_PUBLISHED
    async with session_factory() as session:
        run = await session.get(AgentRun, uuid.UUID(run_id))
    assert run.finished_at is not None


async def test_updated_at_advances_on_state_change(session_factory):
    """updated_at 是人工裁决时判断"卡了多久"的唯一依据。

    created_at 在重试过的行上不会刷新（重试走 UPDATE 复用同一行），所以没有
    updated_at 就无从判断一个 publish_unknown 是刚发生还是躺了三周。
    """
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)
    async with session_factory() as session:
        claimed = await get_published_post(session, run_id)

    async with session_factory() as session:
        await note_publish_unknown(session, agent_run_id=run_id, error="timeout")
    async with session_factory() as session:
        unknown = await get_published_post(session, run_id)

    assert unknown.updated_at > claimed.updated_at


async def test_manual_confirm_only_applies_once(session_factory):
    """两次裁决只有第一次生效：第二次拿到 0 行。"""
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)

    async with session_factory() as session:
        assert (
            await confirm_published_manually(session, agent_run_id=run_id, x_post_id=POST_ID)
            is True
        )
    async with session_factory() as session:
        assert (
            await confirm_published_manually(session, agent_run_id=run_id, x_post_id="999999")
            is False
        )

    async with session_factory() as session:
        row = await get_published_post(session, run_id)
    assert row.x_post_id == POST_ID


async def test_reopen_requires_publishing(session_factory):
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)
    async with session_factory() as session:
        await mark_published(session, agent_run_id=run_id, x_post_id=POST_ID)

    async with session_factory() as session:
        assert await reopen_publish_for_retry(session, agent_run_id=run_id) is False

    assert await _status(session_factory, run_id) == STATUS_PUBLISHED


async def test_unknown_row_is_not_a_dedup_basis(session_factory):
    """留在 publishing 的行不会污染 Guardrail 判重（list_published_contents 的过滤）。"""
    run_id, draft_id = await _make_run_and_draft(session_factory)
    await _claim(session_factory, run_id, draft_id)

    async with session_factory() as session:
        assert await list_published_contents(session) == []
