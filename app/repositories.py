import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Draft, PublishedPost, ResearchItem, Review
from app.models.published_post import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_UNKNOWN,
    UNRESOLVED_STATUSES,
    UQ_AGENT_RUN_ID,
)
from app.schemas import ResearchResult

logger = logging.getLogger(__name__)

# 允许发起发布的 Run 状态：approved（照发原稿）/ edited（发人工改写稿）。
# rejected 不在其中——被拒绝的内容绝不允许发布。
PUBLISHABLE_RUN_STATUSES = ("approved", "edited")

# review 表的人工决定 → 它对应的可发布 Run 状态。
# 发布失败要把 Run 还原成审核留下的结论，还原值只能从这里取，不能猜。
REVIEW_STATUS_TO_RUN_STATUS = {"approve": "approved", "edit": "edited"}

# Run 真正终结的状态：只有这些才写 finished_at。
# approved / edited **不是**终态——它们还要走发布，因此 finished_at 保持为空。
# waiting_for_review / publishing / publish_unknown 同理。
TERMINAL_RUN_STATUSES = ("published", "rejected", "failed", "completed")


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

    finished_at 只在终结态才写：approve / edit 之后 Run 还要走发布，此时
    "流水线结束"不等于"这个 Run 结束了"，写时间戳会让 GET 的语义含混不清
    （见 TERMINAL_RUN_STATUSES 的说明）。
    """
    result = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == _uuid(run_id), AgentRun.status == expected)
        .values(
            status=new_status,
            finished_at=_now() if new_status in TERMINAL_RUN_STATUSES else None,
            error=None,
        )
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
    """对外真正发布过的正文，用于 Guardrail 的完全重复检查。

    必须过滤 `x_post_id IS NOT NULL`：Phase 4 起 published_post 里的行可能是
    publishing（结果未知）或 failed（确定没发出去）的尝试记录，它们**从未出现在 X 上**。
    把它们算进"已发布"会让 Guardrail 拿没发出去的内容去判重，误报 duplicate。
    """
    result = await session.execute(
        select(PublishedPost.body).where(PublishedPost.x_post_id.is_not(None))
    )
    return list(result.scalars().all())


def _unique_violation_constraint(exc: IntegrityError) -> str | None:
    """返回被违反的唯一约束名；不是唯一约束违规则返回 None。

    刻意不把 IntegrityError 一律当成"并发冲突"：外键违规（draft_id 指向不存在的
    草稿）是真正的程序缺陷，必须冒泡成 500 而不是伪装成 409 被吞掉。
    用 getattr 取 constraint_name 而不 import psycopg，保持本模块不绑定驱动。
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None


async def get_published_post(session: AsyncSession, agent_run_id: str) -> PublishedPost | None:
    result = await session.execute(
        select(PublishedPost).where(PublishedPost.agent_run_id == _uuid(agent_run_id))
    )
    return result.scalars().first()


async def claim_publish(
    session: AsyncSession, *, agent_run_id: str, draft_id: str, body: str
) -> bool:
    """原子地抢占一次发布尝试，返回是否抢到。抢到才允许调用 X。

    必须在调用 X **之前**单独提交（本函数自带 commit）：崩溃时宁可留下
    "以为发出去了"的痕迹由人工裁决，也不能没有任何痕迹——后者恢复时会当成
    "从没发过"而重复发推。这是刻意的失败方向选择。

    **并发的主要串行化点是 agent_run 的条件 UPDATE**：两个请求都改同一行，
    PostgreSQL 行锁让后来者排队，等前者提交后按 READ COMMITTED 重检 WHERE，
    拿到 0 行 → 直接拒绝。这一步与写发布行在**同一个事务**里，所以不存在
    "Run 已进入 publishing 但没有对应发布行"的中间态。

    发布行本身仍是"写入自带条件"，没有"先 SELECT 再决定"：
    - 重试路径：条件 UPDATE（WHERE status='failed'）复用同一行
    - 首次路径：INSERT，靠 UNIQUE(agent_run_id) 守护"一个 Run 最多一条记录"

    唯一约束在这里是**第二道防线**：Run 层的 CAS 已经挡住了并发，它守的是
    schema 级不变量，防的是绕过本函数的写入。
    """
    run_uuid = _uuid(agent_run_id)

    claimed = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run_uuid, AgentRun.status.in_(PUBLISHABLE_RUN_STATUSES))
        .values(status=STATUS_PUBLISHING, finished_at=None)
    )
    if claimed.rowcount != 1:
        # 已 published / 仍在 publishing / publish_unknown / 还没审核 —— 一律拒绝。
        # 这里也顺带挡住了"结果未知时再次发布"。
        await session.rollback()
        return False

    reused = await session.execute(
        update(PublishedPost)
        .where(
            PublishedPost.agent_run_id == run_uuid,
            PublishedPost.status == STATUS_FAILED,
        )
        .values(
            status=STATUS_PUBLISHING,
            body=body,
            draft_id=_uuid(draft_id),
            last_error=None,  # 新一次尝试，上一次的错误已经过时
        )
    )
    if reused.rowcount == 1:
        await session.commit()
        return True

    # 没有可复用的 failed 行 → 走首次插入
    session.add(
        PublishedPost(
            agent_run_id=run_uuid,
            draft_id=_uuid(draft_id),
            body=body,
            status=STATUS_PUBLISHING,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()  # Run 的 CAS 一并回滚，不会留下半个发布态
        if _unique_violation_constraint(exc) == UQ_AGENT_RUN_ID:
            return False
        raise  # 其他约束（如外键）是程序缺陷，不在这里吞掉
    return True


async def _revert_run_to_review_outcome(
    session: AsyncSession, agent_run_id: str, *, expected: tuple[str, ...]
) -> bool:
    """把 Run 从发布态还原成审核留下的结论（approved / edited）。

    还原值取自 review 表——它是人工决定的业务事实来源。刻意不从调用方传入：
    调用方（HTTP 请求）可能早就被取消了，而 review 行是不可变的。

    取不到映射时**什么都不做**并返回 False，而不是猜一个。猜错的后果是放行一次
    本不该发生的发布；停在发布态的后果只是后续发布被 409 挡住。两者轻重不同。
    （实际上不可达：发布前置校验已要求 review.status ∈ {approve, edit}，而 Run
    一旦离开 waiting_for_review 就不可能再被改写审核结论。）
    """
    review = await get_review_for_run(session, agent_run_id)
    target = REVIEW_STATUS_TO_RUN_STATUS.get(getattr(review, "status", None))
    if target is None:
        return False
    result = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == _uuid(agent_run_id), AgentRun.status.in_(expected))
        .values(status=target, finished_at=None)
    )
    return result.rowcount == 1


async def mark_published(session: AsyncSession, *, agent_run_id: str, x_post_id: str) -> bool:
    """X 返回 2xx：确凿事实，写入 post_id，并把 Run 推进到终态。

    条件放宽到 IN (publishing, failed)：X 的 2xx 是事实，而人工可能在我们飞行途中
    把状态 resolve 成了 failed（那是基于过期观察的猜测）。**证据压过猜测**——
    否则会出现"推文已经发出去了，但库里记着 failed"→ 下次重发就是重复推文。
    排除 published 是为了不覆盖已经落库的成功结果。

    Run 侧同理放宽到"任何未发布的发布态或可发布态"：既然确定了已发出，
    Run 就必须落在 published，不受任何人此前的猜测影响。
    """
    result = await session.execute(
        update(PublishedPost)
        .where(
            PublishedPost.agent_run_id == _uuid(agent_run_id),
            PublishedPost.status.in_((STATUS_PUBLISHING, STATUS_FAILED)),
        )
        .values(
            status=STATUS_PUBLISHED,
            x_post_id=x_post_id,
            published_at=_now(),
            last_error=None,
        )
    )
    if result.rowcount != 1:
        await session.rollback()
        return False
    await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == _uuid(agent_run_id),
            AgentRun.status.in_((*UNRESOLVED_STATUSES, *PUBLISHABLE_RUN_STATUSES)),
        )
        .values(status=STATUS_PUBLISHED, finished_at=_now())
    )
    await session.commit()
    return True


async def mark_publish_failed(session: AsyncSession, *, agent_run_id: str, error: str) -> bool:
    """X 明确拒绝（4xx）：确定没发出去，可以重试。

    条件收窄到只改 publishing：与 mark_published 相反，**猜测不能覆盖事实**——
    不能把人工已确认的结论（或已经成功的结果）改写成失败。

    Run 一起回退到审核结论。这是"发布失败不是流水线失败"的落点：
    Run 只会回到 approved / edited，**永远不写 failed**。
    """
    result = await session.execute(
        update(PublishedPost)
        .where(
            PublishedPost.agent_run_id == _uuid(agent_run_id),
            PublishedPost.status == STATUS_PUBLISHING,
        )
        .values(status=STATUS_FAILED, last_error=error)
    )
    if result.rowcount != 1:
        await session.rollback()
        return False
    reverted = await _revert_run_to_review_outcome(
        session, agent_run_id, expected=(STATUS_PUBLISHING,)
    )
    await session.commit()
    if not reverted:  # pragma: no cover - 见 _revert_run_to_review_outcome 的不可达说明
        logger.error("publish failed but run could not be reverted (run_id=%s)", agent_run_id)
    return True


async def note_publish_unknown(session: AsyncSession, *, agent_run_id: str, error: str) -> None:
    """结果未知（超时 / 5xx / 无法判定）：转 publish_unknown，只留一行说明。

    `publishing` 与 `publish_unknown` 的区别在**我们手上有什么信息**：
    前者是"本地对结果一无所知"（崩溃 / 仍在飞行中），后者是"尝试已经结束、
    拿到了信息却无法判定"。两者都绝不允许自动重试，但运维含义不同。

    没有把状态改成任何一个确定值，是因为自动改成 published / failed 都会让
    后续处理做出错误决定——这正是本阶段要防的事。
    """
    result = await session.execute(
        update(PublishedPost)
        .where(
            PublishedPost.agent_run_id == _uuid(agent_run_id),
            PublishedPost.status == STATUS_PUBLISHING,
        )
        .values(status=STATUS_UNKNOWN, last_error=error)
    )
    if result.rowcount != 1:
        # 人工已经裁决过了（或行已被别人推进）→ 不动 Run，尊重既有结论
        await session.rollback()
        return
    await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == _uuid(agent_run_id),
            AgentRun.status == STATUS_PUBLISHING,
        )
        .values(status=STATUS_UNKNOWN)
    )
    await session.commit()


async def confirm_published_manually(
    session: AsyncSession, *, agent_run_id: str, x_post_id: str
) -> bool:
    """人工裁决：上 X 看了，确实发出去了 → 补上 x_post_id，转 published。

    接受 publishing 与 publish_unknown 两种入口：裁决的前提是"人已经上 X 看过"，
    这两者都需要人去看，也都需要同一个出口。
    """
    result = await session.execute(
        update(PublishedPost)
        .where(
            PublishedPost.agent_run_id == _uuid(agent_run_id),
            PublishedPost.status.in_(UNRESOLVED_STATUSES),
        )
        .values(
            status=STATUS_PUBLISHED,
            x_post_id=x_post_id,
            published_at=_now(),
            last_error=None,
        )
    )
    if result.rowcount != 1:
        await session.rollback()
        return False
    await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == _uuid(agent_run_id),
            AgentRun.status.in_(UNRESOLVED_STATUSES),
        )
        .values(status=STATUS_PUBLISHED, finished_at=_now())
    )
    await session.commit()
    return True


async def reopen_publish_for_retry(session: AsyncSession, *, agent_run_id: str) -> bool:
    """人工裁决：上 X 看了，确实没发出去 → 改回 failed 以便重发。

    本函数不发帖。真正重发必须另外显式调一次 POST /publish（两道确认）。
    Run 同步回退成审核结论，让它重新处在"可发布"状态。
    """
    result = await session.execute(
        update(PublishedPost)
        .where(
            PublishedPost.agent_run_id == _uuid(agent_run_id),
            PublishedPost.status.in_(UNRESOLVED_STATUSES),
        )
        .values(status=STATUS_FAILED, last_error="manually resolved: not found on X")
    )
    if result.rowcount != 1:
        await session.rollback()
        return False
    reverted = await _revert_run_to_review_outcome(
        session, agent_run_id, expected=UNRESOLVED_STATUSES
    )
    await session.commit()
    if not reverted:  # pragma: no cover - 见 _revert_run_to_review_outcome 的不可达说明
        logger.error("publish reopened but run could not be reverted (run_id=%s)", agent_run_id)
    return True


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
