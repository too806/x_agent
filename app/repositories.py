import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Draft, ResearchItem
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
