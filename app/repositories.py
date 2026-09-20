import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Draft, PublishedPost, ResearchItem, Review
from app.schemas import ResearchResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value: str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


async def create_agent_run(session: AsyncSession, topic: str) -> AgentRun:
    run = AgentRun(topic=topic, status="running", started_at=_now())
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def mark_run_completed(session: AsyncSession, run_id: str) -> None:
    run = await session.get(AgentRun, _uuid(run_id))
    if run is not None:
        run.status = "completed"
        run.finished_at = _now()
        await session.commit()


async def mark_run_failed(session: AsyncSession, run_id: str, error: str) -> None:
    run = await session.get(AgentRun, _uuid(run_id))
    if run is not None:
        run.status = "failed"
        run.finished_at = _now()
        run.error = error
        await session.commit()


async def create_research_items(
    session: AsyncSession, run_id: str, results: list[ResearchResult]
) -> list[str]:
    run_uuid = _uuid(run_id)
    items = [
        ResearchItem(
            agent_run_id=run_uuid,
            platform_post_id=r.platform_post_id,
            author_username=r.author_username,
            content=r.content,
            source_url=r.url,
            likes=r.likes,
            reposts=r.reposts,
            replies=r.replies,
            published_at=r.published_at,
        )
        for r in results
    ]
    session.add_all(items)
    await session.commit()
    return [str(item.id) for item in items]


async def create_draft(
    session: AsyncSession,
    run_id: str,
    content: str,
    version: int,
    prompt_version: str,
    model_name: str,
) -> str:
    draft = Draft(
        agent_run_id=_uuid(run_id),
        content=content,
        status="draft",
        version=version,
        prompt_version=prompt_version,
        model_name=model_name,
        generated_at=_now(),
    )
    session.add(draft)
    await session.commit()
    await session.refresh(draft)
    return str(draft.id)


async def mark_run_waiting_for_review(session: AsyncSession, run_id: str) -> None:
    """Graph 在 human_review 处 interrupt，Run 进入等待人工状态。

    此时不计 finished_at：流程尚未结束。
    """
    run = await session.get(AgentRun, _uuid(run_id))
    if run is not None:
        run.status = "waiting_for_review"
        await session.commit()


async def claim_run_for_review(
    session: AsyncSession, run_id: str, *, expected: str, new_status: str
) -> bool:
    """原子地把 Run 从 expected 状态改成 new_status，返回是否抢到。

    用带条件的单条 UPDATE 做 compare-and-swap：并发提交两个 review 请求时，
    只有一个能把状态从 waiting_for_review 改走，另一个拿到 0 行、直接拒绝，
    避免同一个 Graph thread 被 resume 两次。不需要额外锁服务。

    error 一并清空：这是新一次尝试的开始，上一次（若失败）的错误已经过时；
    本次若再失败，release_run_review_claim 会重新写入。
    """
    result = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == _uuid(run_id), AgentRun.status == expected)
        .values(status=new_status, finished_at=_now(), error=None)
    )
    await session.commit()
    return result.rowcount == 1


async def release_run_review_claim(
    session: AsyncSession, run_id: str, *, claimed_status: str, error: str
) -> str | None:
    """resume 失败时回退占位状态，返回回退后 Run 的真实状态。

    Graph 在 resume 期间失败时仍停在 human_review（checkpoint 未推进），Run 事实上
    还在等人工，所以状态放回 waiting_for_review、清掉 finished_at，仅保留 error 供
    排查。否则一次瞬时故障（如 PG 瞬断）就会让 Run 永久不可恢复：checkpoint 还在，
    但 review 接口会因为状态不是 waiting_for_review 而一直 409。

    同样用 CAS 限定「只回退本次抢到的那个状态」，不覆盖其他写入。
    """
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id == _uuid(run_id), AgentRun.status == claimed_status)
        .values(status="waiting_for_review", finished_at=None, error=error)
    )
    await session.commit()
    run = await session.get(AgentRun, _uuid(run_id))
    return run.status if run is not None else None


async def get_agent_run(session: AsyncSession, run_id: str) -> AgentRun | None:
    return await session.get(AgentRun, _uuid(run_id))


async def get_draft_for_run(session: AsyncSession, run_id: str) -> Draft | None:
    result = await session.execute(
        select(Draft).where(Draft.agent_run_id == _uuid(run_id)).order_by(Draft.version.desc())
    )
    return result.scalars().first()


async def get_review_for_run(session: AsyncSession, run_id: str) -> Review | None:
    result = await session.execute(
        select(Review)
        .join(Draft, Review.draft_id == Draft.id)
        .where(Draft.agent_run_id == _uuid(run_id))
        .order_by(Review.created_at.desc())
    )
    return result.scalars().first()


async def list_published_contents(session: AsyncSession) -> list[str]:
    """已发布正文，用于 Guardrail 的完全重复检查（Phase 3 尚无发布，通常为空）。"""
    result = await session.execute(select(PublishedPost.body))
    return list(result.scalars().all())


async def create_review(
    session: AsyncSession,
    *,
    draft_id: str | None,
    action: str,
    original_content: str,
    edited_content: str | None = None,
    reject_reason: str | None = None,
) -> str | None:
    """保存人工审核决定。原 draft 行不在这里改动。

    一个 Draft 始终只有一条决定：resume 失败后重试会重放 human_review 节点，若上次
    已经写成功（checkpoint 写失败但本行已提交），这里复用并覆盖那一行，而不是插入
    第二条。
    """
    if draft_id is None:
        return None
    draft_uuid = _uuid(draft_id)
    existing = (
        (await session.execute(select(Review).where(Review.draft_id == draft_uuid)))
        .scalars()
        .first()
    )
    review = existing if existing is not None else Review(draft_id=draft_uuid)
    review.status = action
    review.original_content = original_content
    review.edited_content = edited_content
    review.reject_reason = reject_reason
    review.reviewed_at = _now()
    session.add(review)
    await session.commit()
    await session.refresh(review)
    return str(review.id)
