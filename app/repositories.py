import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import case, func, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentDecision,
    AgentRun,
    AutoReview,
    Draft,
    LearningProposalRecord,
    PostMetricsSnapshot,
    PublishedPost,
    ResearchItem,
    Review,
)
from app.models.agent_run import ORIGIN_MANUAL, ORIGIN_SCHEDULED
from app.models.auto_review import (
    UQ_AUTO_REVIEW_DRAFT,
    VERDICT_APPROVE,
    VERDICT_REJECT,
    AutoReview,
)
from app.models.learning_proposal import UQ_LEARNING_PROPOSAL_KEY
from app.models.post_metrics_snapshot import UQ_X_POST_OBSERVED
from app.models.review import DECIDED_BY_HUMAN
from app.models.style_profile import StyleProfile
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


async def create_agent_run(
    session: AsyncSession,
    topic: str | None,
    *,
    goal: str | None = None,
    origin: str = ORIGIN_MANUAL,
) -> AgentRun:
    """新建一个 Run。

    `topic` 与 `goal` 二选一：
    - 走既有 topic 链路的调用方只传 topic（行为与 Phase 1–4a 逐字相同）；
    - 走 Phase 5A Agent 链路的调用方只传 goal，topic 留空等 Agent 自己发现。

    两条都允许为空是刻意的：Run 行必须能先建出来，Agent 才知道自己有 run_id
    可用作 thread_id。topic 为空的那段时间是真实存在的中间态，不是错误状态。

    `origin` 默认 `manual`——**不给默认值之外的行为留余地**：既有的每一个调用方
    （控制台、smoke 脚本）都确实是人发起的，它们一行都不用改。自动调度器显式传
    `ORIGIN_SCHEDULED`，于是"谁建的"是一个被写下来的事实，不是事后猜出来的。
    """
    if (topic is None) == (goal is None):
        raise ValueError("exactly one of topic / goal must be provided")
    run = AgentRun(
        topic=topic, goal=goal, status="running", started_at=_now(), origin=origin
    )
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
            quote_count=r.quote_count,
            published_at=r.published_at,
            # 三个时间原样落库，不做任何兜底：`observed_at` 缺失就是缺失
            # （老数据、或调用方没提供），拿 `published_at` 或插入时刻顶上
            # 等于把"没观察到"伪装成"观察过"。
            observed_at=r.observed_at,
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


async def get_latest_corpus_profile(session: AsyncSession) -> StyleProfile | None:
    """最近一次算出的**语料级** Style Profile（Phase 5B.2）。

    只取 `scope='corpus'`：Account Profile 也照常计算与存储，但运行期**不自动
    挑某一个账号**（工作单第十八、三十二节——学写法的前提是知道在学谁，
    而"系统替我挑了一个账号"会让这件事重新变得说不清）。

    取最新的一版（`computed_at desc`）：Profile 是随语料演进的快照，
    运行期应当用最新那版，而"用了哪一版"由 recipe 的 `profile_ref` 记录，
    事后可回溯。

    没有 Profile 时返回 None —— 那是**正常状态**（还没采过语料），
    不是错误：调用方据此退化成一个不施加结构约束的中性 recipe。
    """
    result = await session.execute(
        select(StyleProfile)
        .where(StyleProfile.scope == "corpus")
        .order_by(StyleProfile.computed_at.desc())
        .limit(1)
    )
    return result.scalars().first()


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
    decided_by: str = DECIDED_BY_HUMAN,
) -> str | None:
    """保存审核决定。原 draft 行不在这里改动。

    一个 Draft 始终只有一条决定：resume 失败后重试会重放 human_review 节点，若上次
    已经写成功（checkpoint 写失败但本行已提交），这里复用并覆盖那一行，而不是插入
    第二条。

    `decided_by` 默认 `human`，由 `human_review_node` 从 resume 值里取。
    人和自动审阅者走的是**同一个函数**——差别只有这一列，这正是"机器人只是替你
    点了批准"这句话在数据上的样子。
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
    review.decided_by = decided_by
    session.add(review)
    await session.commit()
    await session.refresh(review)
    return str(review.id)


# ---------------------------------------------------------------------------
# Phase 5A：Agent Decision Trace
#
# 决策与结果**分两次写**（create_decision 记"打算做什么"，record_decision_result
# 记"实际做成了什么"）。这不是为了省事，而是为了崩溃时的可读性：Agent 在某个
# 动作执行到一半挂掉时，库里必须已经留下"它当时想干什么、为什么"。
# 如果等动作做完再一次性写，挂掉的那一步在 trace 里就完全消失了——
# 而"卡在哪一步"恰恰是排查 Agent 最需要的信息。
# ---------------------------------------------------------------------------


async def create_decision(
    session: AsyncSession,
    *,
    run_id: str,
    step: int,
    action: str,
    reason: str,
    target: str | None,
    input_summary: str,
) -> str:
    """记录一次 Planner 决策（结果留空，等执行完回填）。

    与 `create_review` 同样的"先查后写"：唯一约束 (agent_run_id, step) 是第二道
    防线，这里先复用同一步已有的行，避免一次重放把 UNIQUE 撞成 500。
    """
    run_uuid = _uuid(run_id)
    existing = (
        (
            await session.execute(
                select(AgentDecision).where(
                    AgentDecision.agent_run_id == run_uuid, AgentDecision.step == step
                )
            )
        )
        .scalars()
        .first()
    )
    row = existing if existing is not None else AgentDecision(agent_run_id=run_uuid, step=step)
    row.action = action
    row.reason = reason
    row.target = target
    row.input_summary = input_summary
    if existing is None:
        row.result_summary = ""
    session.add(row)
    await session.commit()
    return str(row.id)


async def record_decision_result(
    session: AsyncSession, *, run_id: str, step: int, result_summary: str
) -> None:
    """回填某一步的执行结果。

    条件 UPDATE 限定 (run_id, step)：只改属于这一步的那一行，不会覆盖别的步骤，
    也就在并发或重放时天然安全。行不存在时静默无事——trace 缺失不该让一次
    已经成功的动作变成失败。
    """
    await session.execute(
        update(AgentDecision)
        .where(AgentDecision.agent_run_id == _uuid(run_id), AgentDecision.step == step)
        .values(result_summary=result_summary)
    )
    await session.commit()


async def list_decisions(session: AsyncSession, run_id: str) -> list[AgentDecision]:
    """某个 Run 的完整决策轨迹，按 step 升序。

    按 step 而不是 created_at 排序：created_at 是写入时刻，重放或补写时可能
    与逻辑顺序不一致，而 step 是决策本身的序号，它才是时间线的真实顺序。
    """
    result = await session.execute(
        select(AgentDecision)
        .where(AgentDecision.agent_run_id == _uuid(run_id))
        .order_by(AgentDecision.step.asc())
    )
    return list(result.scalars().all())


async def set_run_topic(session: AsyncSession, run_id: str, topic: str) -> None:
    """把 Agent 选定的主题写回 Run。

    只在 topic 还是空的时候写：一次运行选定主题之后就不再改它，
    否则运行记录列表里的题目会在运行过程中跳变，而人已经照着旧的那个在看内容了。
    """
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id == _uuid(run_id), AgentRun.topic.is_(None))
        .values(topic=topic)
    )
    await session.commit()


# ---------------------------------------------------------------------------
# 运营控制台（Phase 4b）只读查询
#
# 这一节**只读**，不参与任何状态迁移：Admin API 不得改变 Run / Review / Publish
# 的任何语义，所有写路径继续走上面那些带条件 UPDATE 的函数与现有 REST。
#
# 分页一律在服务器端完成（LIMIT/OFFSET + 独立 COUNT）。刻意不在这里做"取全表再
# 在 Python 里切"——Run 数量会随时间无界增长，那种写法今天能跑，明天就是把库拖垮。
# ---------------------------------------------------------------------------

# Review Queue 的排序方向：最老的排最前。人工审核是工作队列，先进先出才不会被
# 新 Run 淹没（按最新排序的话，积压的旧 Run 会永远沉在列表底部）。
REVIEW_QUEUE_RUN_STATUS = "waiting_for_review"


def _escape_like(value: str) -> str:
    """转义 LIKE 元字符，让 topic 搜索按字面量匹配。

    不转义的话，用户搜一个 `_` 会命中任意单字符、搜 `%` 会命中全部——搜索框会
    表现得像坏了。反斜杠必须**先**替换，否则会把后面两个替换产生的转义符再转一遍。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _run_filter_clauses(
    *,
    status: str | None = None,
    topic: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list:
    """Run 列表的过滤条件。COUNT 与 SELECT 共用，保证两者口径永远一致。

    日期过滤用**半开区间** `[date_from, date_to)`：闭区间会让"截止到今天"漏掉
    今天的数据，而按天分桶的图表用半开区间才不会把边界那一天算两次。
    """
    clauses = []
    if status:
        clauses.append(AgentRun.status == status)
    if topic:
        clauses.append(AgentRun.topic.ilike(f"%{_escape_like(topic)}%", escape="\\"))
    if date_from is not None:
        clauses.append(AgentRun.created_at >= date_from)
    if date_to is not None:
        clauses.append(AgentRun.created_at < date_to)
    return clauses


async def count_runs(
    session: AsyncSession,
    *,
    status: str | None = None,
    topic: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(AgentRun)
        .where(*_run_filter_clauses(status=status, topic=topic, date_from=date_from, date_to=date_to))
    )
    return int(result.scalar() or 0)


async def list_runs_page(
    session: AsyncSession,
    *,
    status: str | None = None,
    topic: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int,
    offset: int,
) -> list[AgentRun]:
    """按创建时间倒序取一页 Run。

    排序加 `id` 做第二键：`created_at` 精度有限，同一秒内创建的多个 Run 若只按
    单键排序，PostgreSQL 没有义务在两次查询间给出相同顺序——翻页时会出现"某条
    记录在第 1 页和第 2 页各出现一次，另一条从未出现过"。
    """
    result = await session.execute(
        select(AgentRun)
        .where(*_run_filter_clauses(status=status, topic=topic, date_from=date_from, date_to=date_to))
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def list_research_items(session: AsyncSession, run_id: str) -> list[ResearchItem]:
    """某个 Run 真实落库的研究条目（不是 Graph state 里的副本）。"""
    result = await session.execute(
        select(ResearchItem)
        .where(ResearchItem.agent_run_id == _uuid(run_id))
        .order_by(ResearchItem.created_at, ResearchItem.id)
    )
    return list(result.scalars().all())


async def reviews_by_run(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Review]:
    """批量取每个 Run 的审核记录，避免列表页 N+1。

    Review 挂在 draft 上而不是 run 上，所以经 draft 反查 run_id。
    同一 draft 理论上只有一条记录（create_review 复用同一行），按 created_at
    升序让"万一有多条"时后写的覆盖先写的，与 get_review_for_run 的取法一致。
    """
    if not run_ids:
        return {}
    result = await session.execute(
        select(Draft.agent_run_id, Review)
        .join(Review, Review.draft_id == Draft.id)
        .where(Draft.agent_run_id.in_(run_ids))
        .order_by(Review.created_at, Review.id)
    )
    return {run_id: review for run_id, review in result.all()}


async def auto_reviews_by_run(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, AutoReview]:
    """批量取每个 Run 的自动审阅记录，避免列表页 N+1。

    与 `reviews_by_run` 同一个形状，但**不经过 draft 反查**：`auto_review` 自己
    就有 `agent_run_id`（它是为这个功能建的表，没有既有的形状要迁就）。

    一个 Run 至多一条：`UNIQUE(draft_id)` 加上"一个 Run 一条草稿"，
    所以这里不需要像 `reviews_by_run` 那样担心"万一有多条"。

    只给控制台读，**不参与任何判据**——判据读的是 `checks` 的原始取值，
    而这个函数返回的是给人看的行。
    """
    if not run_ids:
        return {}
    result = await session.execute(
        select(AutoReview).where(AutoReview.agent_run_id.in_(run_ids))
    )
    return {row.agent_run_id: row for row in result.scalars().all()}


async def published_by_run(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, PublishedPost]:
    """批量取每个 Run 的发布记录。没有记录的 Run 不出现在返回的字典里。

    调用方必须把"缺失"当作"没有发布记录"（UI 显示 `—`），**不得**用 Run 状态
    反推一个发布状态——`published_post.status` 才是发布状态的权威。
    """
    if not run_ids:
        return {}
    result = await session.execute(
        select(PublishedPost).where(PublishedPost.agent_run_id.in_(run_ids))
    )
    return {row.agent_run_id: row for row in result.scalars().all()}


async def drafts_by_run(session: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, Draft]:
    """批量取每个 Run 的最新版本草稿（与 get_draft_for_run 的取法一致）。"""
    if not run_ids:
        return {}
    result = await session.execute(
        select(Draft)
        .where(Draft.agent_run_id.in_(run_ids))
        .order_by(Draft.agent_run_id, Draft.version.asc(), Draft.id.asc())
    )
    return {row.agent_run_id: row for row in result.scalars().all()}


async def count_published_grouped_by_status(session: AsyncSession) -> dict[str, int]:
    result = await session.execute(
        select(PublishedPost.status, func.count()).group_by(PublishedPost.status)
    )
    return {status: int(count) for status, count in result.all()}


async def count_reviews_grouped_by_status(session: AsyncSession) -> dict[str, int]:
    """各审核决定的条数。**只数人的决定。**

    正式模式下自动侧也往 `review` 表写行（`decided_by='auto'`，见
    `app/services/auto_publish.py::_publish`），而这条查询喂的是控制台
    analytics 上那块标着"一次人工决定一行"的分布。不过滤的话，转正式之后
    机器替人点的每一次"批准"都会记进人工决策分布里——那个数字从此不再回答
    "人做了什么决定"。
    """
    result = await session.execute(
        select(Review.status, func.count())
        .where(Review.decided_by == DECIDED_BY_HUMAN)
        .group_by(Review.status)
    )
    return {status: int(count) for status, count in result.all()}


async def _daily_counts(session: AsyncSession, column, *, date_from: datetime) -> dict[str, int]:
    """按 UTC 自然日分桶计数。

    显式 `timezone('UTC', ...)` 而不是直接 date_trunc：date_trunc 作用在
    timestamptz 上时用的是**会话时区**，同一个库在不同机器上会给出不同的分桶结果。
    图的横轴必须是确定的，所以先把时间戳归一化到 UTC 再截断。
    """
    bucket = func.date_trunc("day", func.timezone("UTC", column))
    result = await session.execute(
        select(bucket, func.count()).where(column >= date_from).group_by(bucket).order_by(bucket)
    )
    return {day.date().isoformat(): int(count) for day, count in result.all()}


async def daily_run_counts(session: AsyncSession, *, date_from: datetime) -> dict[str, int]:
    return await _daily_counts(session, AgentRun.created_at, date_from=date_from)


async def daily_published_counts(session: AsyncSession, *, date_from: datetime) -> dict[str, int]:
    """按 published_post.created_at 分桶——它是**尝试发起**的时刻。

    刻意不用 published_at：那只有 `published` 的行才有值，failed / unknown 会
    整条从趋势里消失，图会看起来比现实乐观。
    """
    return await _daily_counts(session, PublishedPost.created_at, date_from=date_from)


async def topic_distribution(session: AsyncSession, *, limit: int) -> list[tuple[str, int]]:
    """按 topic 聚合 Run，**排除 `topic IS NULL`**。

    `topic` 允许为空是 Phase 5A 引入的正常状态而不是脏数据：Agent 链路在选定主题
    之前就先创建 Run（`create_agent_run(goal=..., topic=NULL)`，见
    `docs/phase-5a-agent-brain-design.md`），主题是 Agent 自己选的、选完才回填。

    排除而不是造一个替代值，有两个理由：
    - 对外形状 `TopicCount.topic` 是 `str`（analytics 响应的一部分），NULL 装不进去，
      这正是 `/api/admin/analytics` 之前 500 的原因——分组把 NULL 带进了
      `TopicCount(topic=None)`；
    - "还没有主题"不是一个 topic。"未知"之类的占位会把一个**运行中的临时状态**
      说成一种内容类别，读图的人会以为系统产过这类主题。

    代价是正在选题的那部分 Run 暂时不计入这张图——它们仍然计入 `run_volume`，
    而这张图问的是"已产出的内容分布在哪些主题上"。
    """
    result = await session.execute(
        select(AgentRun.topic, func.count())
        .where(AgentRun.topic.is_not(None))
        .group_by(AgentRun.topic)
        .order_by(func.count().desc(), AgentRun.topic.asc())
        .limit(limit)
    )
    return [(topic, int(count)) for topic, count in result.all()]


async def average_run_duration(session: AsyncSession) -> tuple[float | None, int, int]:
    """返回 (平均秒数, 参与计算的 Run 数, Run 总数)。

    只有**终态** Run 才有 `finished_at`（见 TERMINAL_RUN_STATUSES）：approved /
    edited / waiting_for_review / publishing / publish_unknown 一律为空。因此这个
    平均值只能覆盖有始有终的那部分 Run，调用方必须把样本数与总数一并展示，
    否则读图的人会以为它代表全部 Run。
    """
    result = await session.execute(
        select(
            func.avg(func.extract("epoch", AgentRun.finished_at - AgentRun.started_at)),
            func.count(),
        ).where(AgentRun.finished_at.is_not(None), AgentRun.started_at.is_not(None))
    )
    average, sampled = result.one()
    total = int((await session.execute(select(func.count()).select_from(AgentRun))).scalar() or 0)
    return (float(average) if average is not None else None, int(sampled), total)


async def count_review_queue(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.status == REVIEW_QUEUE_RUN_STATUS)
    )
    return int(result.scalar() or 0)


async def list_review_queue(session: AsyncSession, *, limit: int, offset: int) -> list[AgentRun]:
    """待人工审核的 Run，最老的在前（先进先出，见 REVIEW_QUEUE_RUN_STATUS 说明）。"""
    result = await session.execute(
        select(AgentRun)
        .where(AgentRun.status == REVIEW_QUEUE_RUN_STATUS)
        .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def count_unresolved_publications(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(PublishedPost)
        .where(PublishedPost.status.in_(UNRESOLVED_STATUSES))
    )
    return int(result.scalar() or 0)


async def list_unresolved_publications(
    session: AsyncSession, *, limit: int
) -> list[PublishedPost]:
    """需要人工裁决的发布记录。

    `publish_unknown` 排在 `publishing` 前面：前者是一次**已经结束**的尝试，
    人去看一眼就能裁决；后者可能还在飞行中，立刻去 X 上找反而可能找不到。
    先把能立刻处理的排前面。
    """
    priority = case((PublishedPost.status == STATUS_UNKNOWN, 0), else_=1)
    result = await session.execute(
        select(PublishedPost)
        .where(PublishedPost.status.in_(UNRESOLVED_STATUSES))
        .order_by(priority, PublishedPost.updated_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def _published_filter_clauses(
    *,
    status: str | None = None,
    unresolved: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list:
    clauses = []
    if status:
        clauses.append(PublishedPost.status == status)
    if unresolved:
        # 与 status 是**取交集**而不是互相覆盖：两个条件都是"再收窄一层"，
        # 传入不冲突的组合（比如 status=published + unresolved）得到空页，
        # 这比让其中一个悄悄失效要好猜。
        clauses.append(PublishedPost.status.in_(UNRESOLVED_STATUSES))
    if date_from is not None:
        clauses.append(PublishedPost.created_at >= date_from)
    if date_to is not None:
        clauses.append(PublishedPost.created_at < date_to)
    return clauses


async def count_published(
    session: AsyncSession,
    *,
    status: str | None = None,
    unresolved: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(PublishedPost)
        .where(
            *_published_filter_clauses(
                status=status, unresolved=unresolved, date_from=date_from, date_to=date_to
            )
        )
    )
    return int(result.scalar() or 0)


async def list_published_page(
    session: AsyncSession,
    *,
    status: str | None = None,
    unresolved: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int,
    offset: int,
) -> list[PublishedPost]:
    """发布记录列表。

    默认按创建时间倒序（"最近发生了什么"是列表的常规读法）。`unresolved=True`
    时改用优先级排序：`publish_unknown` 在最前——它意味着一条推文**可能已经发出去了
    却没人知道**，是这四种状态里唯一会随时间变得更糟的。`publishing` 次之（可能还在
    飞行中）。这两种状态都在等同一个人的决定。
    """
    order_by = [PublishedPost.created_at.desc(), PublishedPost.id.desc()]
    if unresolved:
        # 与 list_unresolved_publications 保持同一套优先级，避免两处"待裁决"队列
        # 给出的顺序不一样。
        priority = case(
            (PublishedPost.status == STATUS_UNKNOWN, 0),
            (PublishedPost.status == STATUS_PUBLISHING, 1),
            else_=2,
        )
        order_by = [priority, PublishedPost.created_at.asc()]

    result = await session.execute(
        select(PublishedPost)
        .where(
            *_published_filter_clauses(
                status=status, unresolved=unresolved, date_from=date_from, date_to=date_to
            )
        )
        .order_by(*order_by)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_runs_by_ids(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, AgentRun]:
    """批量取 Run，供 Published / Resolution 列表补上 topic 等展示字段。"""
    if not run_ids:
        return {}
    result = await session.execute(select(AgentRun).where(AgentRun.id.in_(run_ids)))
    return {row.id: row for row in result.scalars().all()}


# ---------------------------------------------------------------------------
# Phase 5B.4 —— post_metrics_snapshot（只追加）
# ---------------------------------------------------------------------------


async def insert_post_metrics_snapshot(
    session: AsyncSession,
    *,
    published_post_id: uuid.UUID,
    x_post_id: str,
    observed_at: datetime,
    hours_since_publish: float,
    likes: int | None,
    replies: int | None,
    reposts: int | None,
    quotes: int | None,
    impressions: int | None = None,
    bookmarks: int | None = None,
) -> PostMetricsSnapshot | None:
    """追加一条抓取记录。返回 None 表示**这个时刻已经记过**（幂等跳过）。

    用 `ON CONFLICT DO NOTHING` 按 (x_post_id, observed_at) 去重，而不是先查后插：
    同一个脚本被跑两遍（或两次抓取落在同一微秒）不该产生第二行，但也不该报错——
    快照是只追加的事实，重复抓同一时刻没有新信息。

    计数一律**原样**写入，包括 None。调用方不得把"X 没返回这个字段"翻译成 0：
    0 是一个观测值，NULL 才是"没拿到"，两者在下游（rate 的分母、报告的口径）
    含义完全不同。
    """
    stmt = (
        pg_insert(PostMetricsSnapshot)
        .values(
            published_post_id=published_post_id,
            x_post_id=x_post_id,
            observed_at=observed_at,
            hours_since_publish=hours_since_publish,
            likes=likes,
            replies=replies,
            reposts=reposts,
            quotes=quotes,
            impressions=impressions,
            bookmarks=bookmarks,
        )
        .on_conflict_do_nothing(constraint=UQ_X_POST_OBSERVED)
        .returning(PostMetricsSnapshot.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    await session.commit()
    if inserted_id is None:
        return None
    return await session.get(PostMetricsSnapshot, inserted_id)


async def list_published_posts_with_x_id(
    session: AsyncSession, *, limit: int | None = None
) -> list[PublishedPost]:
    """抓取脚本的输入：**已确认发布**且拿得到 x_post_id 的帖子。

    只取 status=published：`publishing` / `publish_unknown` 的 x_post_id 可能是
    NULL，而 `failed` 确定没发出去。published_at 也必须非空，否则算不出
    `hours_since_publish`——"不知道发布了多久"的帖子不进快照表，而不是存一个
    NULL 年龄。两个条件都在这里过滤，调用方无需再判。
    """
    stmt = (
        select(PublishedPost)
        .where(
            PublishedPost.status == STATUS_PUBLISHED,
            PublishedPost.x_post_id.is_not(None),
            PublishedPost.published_at.is_not(None),
        )
        .order_by(PublishedPost.published_at.asc(), PublishedPost.id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_published_post_by_id(
    session: AsyncSession, published_post_id: uuid.UUID | str
) -> PublishedPost | None:
    """按发布行自己的主键取。

    与 `get_published_post` 的分工：那个是"这个 Run 发过没有"（按 agent_run_id），
    这个的调用方手上拿的已经是快照行里的 `published_post_id`——
    反馈侧只有这个 id，回不到 agent_run_id 去找。
    """
    result = await session.execute(
        select(PublishedPost).where(PublishedPost.id == _uuid(published_post_id))
    )
    return result.scalars().first()


async def list_post_metrics_snapshots(
    session: AsyncSession,
    *,
    published_post_id: uuid.UUID | None = None,
    x_post_id: str | None = None,
    limit: int | None = None,
) -> list[PostMetricsSnapshot]:
    """按时间正序取快照。时间序列的读法就是正序（Day 0 → Day 7）。"""
    stmt = select(PostMetricsSnapshot)
    if published_post_id is not None:
        stmt = stmt.where(PostMetricsSnapshot.published_post_id == published_post_id)
    if x_post_id is not None:
        stmt = stmt.where(PostMetricsSnapshot.x_post_id == x_post_id)
    stmt = stmt.order_by(
        PostMetricsSnapshot.observed_at.asc(), PostMetricsSnapshot.id.asc()
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_recent_post_metrics_snapshots(
    session: AsyncSession, *, limit: int
) -> list[PostMetricsSnapshot]:
    """**最近** limit 条观察，按 `observed_at` 正序返回。

    与 `list_post_metrics_snapshots` 的差别只在"截断的是哪一头"：
    那个函数的 `limit` 作用在正序的第 N 条，取到的是最早的一批；
    反馈报告要的是最近的一批（"我们这个月发出去的东西后来怎么样"），
    所以先按倒序取、再翻回正序，让调用方拿到的仍然是一条时间线。
    """
    stmt = (
        select(PostMetricsSnapshot)
        .order_by(PostMetricsSnapshot.observed_at.desc(), PostMetricsSnapshot.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    rows.reverse()
    return rows


async def count_post_metrics_snapshots(
    session: AsyncSession, *, published_post_id: uuid.UUID | None = None
) -> int:
    stmt = select(func.count()).select_from(PostMetricsSnapshot)
    if published_post_id is not None:
        stmt = stmt.where(PostMetricsSnapshot.published_post_id == published_post_id)
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def latest_metrics_observed_at(session: AsyncSession) -> datetime | None:
    """最近一次抓取时刻。用来回答"这份反馈有多新"——没有快照时是 None，不是 now()。"""
    result = await session.execute(select(func.max(PostMetricsSnapshot.observed_at)))
    return result.scalar_one_or_none()


async def latest_metrics_by_post(
    session: AsyncSession, post_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[PostMetricsSnapshot, int]]:
    """一页帖子的**最新观察 + 观察次数**，一次查完。

    发布列表是要分页的，所以这里**不能**逐条去查最新快照——20 行就是 20 次往返，
    而且每次都是同一句 SQL。用 `DISTINCT ON` 一次拿到每帖最新那条，
    再一条 `GROUP BY` 拿计数，总共两次查询，与页大小无关。

    返回 `{published_post_id: (最新快照, 观察次数)}`。

    两条纪律：

    - **没有快照的帖子不出现在返回里**，而不是出现一个计数为 0 的条目。
      调用方据此渲染"暂无数据"，这与"采到了，是 0"必须长得不一样。
    - `DISTINCT ON` 的排序键必须与 `ORDER BY` 前缀一致，且后接
      `observed_at DESC, id DESC`——加上 id 是为了在**同一时刻**有两条时
      （理论上被唯一约束挡住，但读路径不该依赖写路径的约束）仍然取到确定的一条。
    """
    if not post_ids:
        return {}

    latest_stmt = (
        select(PostMetricsSnapshot)
        # `SELECT DISTINCT ON (...)`。用 `.ext(distinct_on(...))` 而不是
        # `.distinct(列)`：后者在 SQLAlchemy 2.1 起已弃用，而漏掉这个警告
        # 只会在将来升级时突然变成一句错误的 SQL。
        .ext(distinct_on(PostMetricsSnapshot.published_post_id))
        .where(PostMetricsSnapshot.published_post_id.in_(post_ids))
        .order_by(
            PostMetricsSnapshot.published_post_id,
            PostMetricsSnapshot.observed_at.desc(),
            PostMetricsSnapshot.id.desc(),
        )
    )
    latest_rows = list((await session.execute(latest_stmt)).scalars().all())

    counts_stmt = (
        select(
            PostMetricsSnapshot.published_post_id,
            func.count().label("observation_count"),
        )
        .where(PostMetricsSnapshot.published_post_id.in_(post_ids))
        .group_by(PostMetricsSnapshot.published_post_id)
    )
    counts = {row[0]: int(row[1]) for row in (await session.execute(counts_stmt)).all()}

    # 计数查询与最新查询用同一组 post_ids，理论上键集一致；用 `get` 兜底而不是
    # `[...]`，是因为这里够不到的东西应该表现为"少一条计数"，而不是让整页 500。
    return {row.published_post_id: (row, counts.get(row.published_post_id, 0)) for row in latest_rows}


# ---------------------------------------------------------------------------
# Phase 5D —— learning_proposal（只追加，而且只记"人做过的裁决"）
# ---------------------------------------------------------------------------


async def insert_learning_proposal_decision(
    session: AsyncSession,
    *,
    proposal_key: str,
    target: str,
    current_value: str,
    proposed_value: str,
    rationale: str,
    evidence_ids: Sequence[str],
    limitations: Sequence[str],
    sample_count: int,
    observation_window: str,
    status: str,
    reviewed_at: datetime,
    current_recipe_version: str | None = None,
    proposed_recipe_version: str | None = None,
    note: str | None = None,
) -> LearningProposalRecord | None:
    """记下**一次**人工裁决。返回 None 表示这条提案**已经被裁决过**。

    用 `ON CONFLICT DO NOTHING` 按 `proposal_key` 去重，而不是先查后插：
    同一个决定被提交两遍（双击、重试、两个标签页）不该产生两行相互矛盾的历史。
    这也让"一条提案只能被裁决一次"成为**数据库层的事实**，而不只是接口层的约定
    ——接口层的约定挡不住并发，唯一键挡得住。

    没有任何 UPDATE 路径：改主意要等下一次观察产生一条新提案，而不是改写
    已经写下的历史。审计记录的意义正在于它不能被后来的事改。

    `evidence_ids` 为空的调用**不该发生**：没有证据的提案在生成阶段就被丢弃了。
    这里不替调用方补一条默认值，也不抛异常——真出现了空列表，那说明写入路径
    漏了一道校验，而那种事应该在测试里被发现，不是在运行期被一个兜底值掩盖。
    """
    stmt = (
        pg_insert(LearningProposalRecord)
        .values(
            proposal_key=proposal_key,
            target=target,
            current_value=current_value,
            proposed_value=proposed_value,
            rationale=rationale,
            evidence_ids=list(evidence_ids),
            limitations=list(limitations),
            sample_count=sample_count,
            observation_window=observation_window,
            current_recipe_version=current_recipe_version,
            proposed_recipe_version=proposed_recipe_version,
            status=status,
            reviewed_at=reviewed_at,
            note=note,
        )
        .on_conflict_do_nothing(constraint=UQ_LEARNING_PROPOSAL_KEY)
        .returning(LearningProposalRecord.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    await session.commit()
    if inserted_id is None:
        return None
    return await session.get(LearningProposalRecord, inserted_id)


async def get_learning_proposal_decision(
    session: AsyncSession, proposal_key: str
) -> LearningProposalRecord | None:
    """按确定性 key 取那一条裁决。没裁决过 → None（**不是**一个 proposed 行）。"""
    result = await session.execute(
        select(LearningProposalRecord).where(
            LearningProposalRecord.proposal_key == proposal_key
        )
    )
    return result.scalars().first()


async def list_learning_proposal_decisions(
    session: AsyncSession,
) -> list[LearningProposalRecord]:
    """全部裁决，按**裁决时刻正序**（同一条提案只有一行，所以顺序是确定的）。

    正序而不是倒序：读它的人要做的是"把裁决叠回现算出来的提案上"，
    叠的顺序不该影响结果；而正序在调试时读起来就是一条时间线。
    这条表会被反复全量读（提案总数是"最多 3 条 / 每个窗口"量级），
    所以不分页、不传上界——真到了需要分页的规模，那是另一个阶段的事。
    """
    stmt = select(LearningProposalRecord).order_by(
        LearningProposalRecord.reviewed_at.asc(), LearningProposalRecord.id.asc()
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_learning_proposal_decisions(
    session: AsyncSession, *, status: str | None = None
) -> int:
    """裁决条数。`status` 给定时只数那一种（'approved' / 'rejected'）。

    **不是**"提案总数"：未裁决的提案不落库，所以这个数字只回答
    "人一共做过多少次裁决"。前端上"有几条提案"读的是现算结果。
    """
    stmt = select(func.count()).select_from(LearningProposalRecord)
    if status is not None:
        stmt = stmt.where(LearningProposalRecord.status == status)
    result = await session.execute(stmt)
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# 自动发布（无人值守）
#
# 这一节里的读取函数都**有上界**，这是刻意的：它们每个 tick 都要跑一次，
# 而没有上界的 `list_published_contents` 会长成账号的整个历史长度。
# 详见各函数的说明。
# ---------------------------------------------------------------------------


async def insert_auto_review(
    session: AsyncSession,
    *,
    agent_run_id: str,
    draft_id: str,
    mode: str,
    verdict: str,
    reasons: list,
    checks: dict,
    critic_score: float | None,
    acted: bool = False,
) -> AutoReview | None:
    """记下一次自动审阅判断。返回 None 表示**这条草稿已经记过**（幂等跳过）。

    用 `ON CONFLICT DO NOTHING` 按 draft_id 去重，而不是先查后插：同一个 tick
    被跑两遍（计划任务与手动端点撞上、或上一步崩了重来）不该产生第二行，
    但也不该报错——**先写的那次说了算**。

    "先写的那次说了算"是有意的：第二次跑时近期的语料已经变了（上一次 tick 可能
    刚发了一条），重算会得到一个不同的结论。让结论随重跑漂移，等于让
    "机器当时判断了什么"变成一个说不清的问题——而影子模式要量的正是它。
    """
    stmt = (
        pg_insert(AutoReview)
        .values(
            agent_run_id=_uuid(agent_run_id),
            draft_id=_uuid(draft_id),
            mode=mode,
            verdict=verdict,
            reasons=reasons,
            checks=checks,
            critic_score=critic_score,
            acted=acted,
        )
        .on_conflict_do_nothing(constraint=UQ_AUTO_REVIEW_DRAFT)
        .returning(AutoReview.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    await session.commit()
    if inserted_id is None:
        return None
    return await session.get(AutoReview, inserted_id)


async def list_recent_published_contents(
    session: AsyncSession, *, since: datetime, limit: int = 50
) -> list[str]:
    """窗口内**真正发布过**的正文，用于跨帖重复检测。有上界。

    与 `list_published_contents` 是两件事，不要互相替代：

    - 那个是 Guardrail 的**全量**判重基准（无界、无窗口），判的是"这条和历史上
      任何一条完全一样吗"；
    - 这个是自动审阅的**近期**语料（有界、有窗口），判的是"这条和最近发过的像不像"。
      重复发帖的风险来自"最近"，把三年前的帖子拉进来只会稀释信号。

    同样过滤 `x_post_id IS NOT NULL`——理由见 `list_published_contents`：
    publishing / failed 的行从未出现在 X 上，拿它们当"已发过"是误报。
    """
    stmt = (
        select(PublishedPost.body)
        .where(
            PublishedPost.x_post_id.is_not(None),
            PublishedPost.published_at.is_not(None),
            PublishedPost.published_at >= since,
        )
        .order_by(PublishedPost.published_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_recent_draft_contents(
    session: AsyncSession,
    *,
    since: datetime,
    limit: int = 50,
    exclude_draft_id: str | None = None,
) -> list[str]:
    """窗口内**产出过**的草稿正文（不分状态）。有上界。

    ## 为什么重复检测必须也看草稿，不能只看已发布

    影子模式下**什么都不发布**，`published_post` 始终是空的。若检测语料只取
    已发布正文，它看到的是一份空语料，于是**放行一切**——影子模式会退化成一个
    永远说"通过"的橡皮图章，量出来的一致率毫无意义。

    取草稿还顺带修了一个正式模式下的漏洞：连续三次生成近乎相同的稿子、
    前两次都被人拒了，第三次照样会被放行。比对"发过什么"看不见这件事，
    比对"写过什么"看得见。

    `exclude_draft_id` 排除本次正在判的那条草稿自己。
    """
    stmt = (
        select(Draft.content)
        .join(AgentRun, Draft.agent_run_id == AgentRun.id)
        .where(
            Draft.created_at >= since,
            Draft.content.is_not(None),
        )
        .order_by(Draft.created_at.desc())
        .limit(limit)
    )
    if exclude_draft_id is not None:
        stmt = stmt.where(Draft.id != _uuid(exclude_draft_id))
    result = await session.execute(stmt)
    return [text for text in result.scalars().all() if text]


async def list_recent_topics(
    session: AsyncSession,
    *,
    since: datetime,
    limit: int = 50,
    exclude_run_id: str | None = None,
) -> list[str]:
    """窗口内**所有状态**的 Run 选定过的题目。有上界。

    刻意**不过滤状态**：一个被拒的 Run 也消耗过那个选题，明天再写一遍同一个题目
    仍然是重复。同 `list_recent_draft_contents`，这条在影子模式下尤其要紧——
    那期间没有任何 Run 会走到 published。

    ## `exclude_run_id` 不是可选的礼貌

    正在被自动审阅的那条 Run **自己的题目已经在库里了**：`finalize` 在
    `set_run_topic` 里把它写进 `agent_run.topic`，而题目检测的输入正是这一列。
    不排除它的话，判据会拿"这条稿子的题目"和"这条稿子的题目"比，
    于是**每一次自动运行都因"最近已经写过同一个题目"被判不通过**。

    那是一种特别难查的失败：影子模式会显示 100% 的误拦，而理由看起来完全合理
    （"确实重复了"），只是没人会想到它是在跟自己比。
    """
    stmt = (
        select(AgentRun.topic)
        .where(
            AgentRun.topic.is_not(None),
            AgentRun.created_at >= since,
        )
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
    )
    if exclude_run_id is not None:
        stmt = stmt.where(AgentRun.id != _uuid(exclude_run_id))
    result = await session.execute(stmt)
    return [topic for topic in result.scalars().all() if topic]


async def count_publish_attempts_since(session: AsyncSession, *, since: datetime) -> int:
    """窗口内向 X 发起过多少次发布**尝试**（成功与否都算）。

    刻意数"尝试"而不是"成功"：`publish_unknown` 的行可能**已经发出去了**
    （见 publish.py 的窗口图）。只数成功会漏掉那些，于是额度在 X 那边看是超的，
    而本地看还没到。这是同一条"证据压过猜测"的老规矩。
    """
    stmt = (
        select(func.count())
        .select_from(PublishedPost)
        .where(PublishedPost.created_at >= since)
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def count_auto_runs_since(session: AsyncSession, *, since: datetime) -> int:
    """窗口内自动调度器建了多少个 Run。

    按 `origin` 而不是按 `goal` 是否等于配置值来数——人可以提交同一个 goal，
    那种推断会把人的 Run 算进机器的额度里。
    """
    stmt = (
        select(func.count())
        .select_from(AgentRun)
        .where(
            AgentRun.origin == ORIGIN_SCHEDULED,
            AgentRun.created_at >= since,
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def reap_stale_review_claims(session: AsyncSession) -> list[str]:
    """回收"已 claim 却没能 resume"的残留 Run，返回被回收的 run_id 列表。

    ## 判据：在 approved/edited 却**没有** review 行

    合法的 `approved` / `edited` 一定有 review 行——它是 `human_review_node`
    在 resume 时写的，而 Run 状态也正是那次 claim 改的。两者出自同一段代码，
    所以"有状态、没记录"只可能是：claim 已提交，resume 还没跑（或崩了）。
    `_preflight` 同样依赖这条不变式，它把缺 review 的 approved 判成 409。

    ## 为什么只收自动发起的 Run

    人和机器在 claim→resume 之间都有一段窗口，但人和机器**不共享那把锁**
    （tick 拿的是 advisory lock，人走 HTTP 不拿）。若这里也去收人的 Run，
    就可能把一个人"正在 resume 的那一次"重置回 `waiting_for_review`——
    他的 resume 随后照常写完 review 行，而 Run 状态已经变了，于是发布时
    撞上"状态与审核结论不符"的 409。

    自动 Run 不存在这个问题：整个 tick 持有 advisory lock，两个 tick 不会并发，
    所以这里看到的残留只可能来自**上一次已经结束（崩溃）的 tick**，
    不可能是"正在进行的那一次"。

    残留窗口本身是无害的（worst case 是人少发一条、要多审一次），但把人的
    Run 卷进来会制造一个新的、更难查的失败，所以不收。

    ## 人那条路径的同一个窗口

    HTTP 路径已经用 `release_run_review_claim` 兜住了异常路径（见 main.py），
    只有"进程整个崩掉"才会留下人的残留。那是既有行为，不在本次改动范围内。
    """
    has_review = (
        select(Review.id)
        .join(Draft, Review.draft_id == Draft.id)
        .where(Draft.agent_run_id == AgentRun.id)
        .exists()
    )
    stmt = select(AgentRun).where(
        AgentRun.origin == ORIGIN_SCHEDULED,
        AgentRun.status.in_(PUBLISHABLE_RUN_STATUSES),
        ~has_review,
    )
    runs = list((await session.execute(stmt)).scalars().all())
    if not runs:
        return []
    reaped: list[str] = []
    for run in runs:
        run.status = REVIEW_QUEUE_RUN_STATUS
        run.error = "review claim was left behind by an interrupted auto tick"
        reaped.append(str(run.id))
    await session.commit()
    return reaped


async def shadow_agreement_stats(session: AsyncSession) -> dict[tuple[str, str], int]:
    """影子模式的混淆矩阵原料：`{(自动结论, 人的决定): 条数}`。

    只统计自动发起的 Run（`origin='scheduled'`）——人自己发起的 Run 没有自动
    判断，混进来会让分母变成两件不同的事。

    返回**原始配对**而不是已经算好的"一致率"：命名与口径是产品决定，
    归服务层（`app/services/auto_review.py::summarize_agreement`）并且要能被
    单测钉住。这里只负责把行数数对。

    只返回**已决**的配对：`review.status` 为 `pending` 的行说明人还没看，
    把"还没决定"算成"不同意"，一致率会凭空变低。

    **只要人的决定（`decided_by='human'`）。** 正式模式下自动侧会重放
    human_review 节点、以 `decided_by='auto'` 留下一条 review 行
    （见 `app/services/auto_publish.py::_publish`）。那条行和它自己写下的
    `auto_review` 按 `draft_id` 天然配得上对，于是一条机器放行的 Run 会
    **白送一个"一致"**。方向是单向的：机器只在判通过时才动作，所以它写的
    每条 review 行都必然是 approve，配上的也必然是 approve——一致率被推向
    1.0，而"一致率 ≥ 90%"正是转正式门槛的第 4 项。漏掉这个过滤，等于让机器
    拿自己的判断给自己打分。影子模式下没有这条行，所以这个错**恰好从翻正
    那一刻开始**，也就是恰好从它开始算数的时刻开始。
    """
    stmt = (
        select(AutoReview.verdict, Review.status, func.count())
        .select_from(AutoReview)
        .join(AgentRun, AutoReview.agent_run_id == AgentRun.id)
        .join(Review, Review.draft_id == AutoReview.draft_id)
        .where(
            AgentRun.origin == ORIGIN_SCHEDULED,
            Review.decided_by == DECIDED_BY_HUMAN,
        )
        .group_by(AutoReview.verdict, Review.status)
    )
    result = await session.execute(stmt)
    return {(verdict, status): int(count) for verdict, status, count in result.all()}


# ---------------------------------------------------------------------------
# 自动发布 tick 的互斥锁
# ---------------------------------------------------------------------------

#: tick 用的 advisory lock 键。任意固定值，全仓只有这一个使用者。
#:
#: 取一个看起来就是"随手挑的常数"的值，而不是 `1`：advisory lock 的命名空间是
#: **整个 PostgreSQL 实例**共享的，别的库、别的项目偶然用了同一个键时，
#: 冲突的表现是 tick 静默地什么都不做——最难查的那类故障。`1` 恰好是
#: 别人最可能也选的值。
AUTO_PUBLISH_LOCK_KEY = 0x5841475F4155544F  # "XAG_AUTO"


async def try_auto_publish_lock(session: AsyncSession) -> bool:
    """尝试独占自动发布的 tick。拿到返回 True，别人正持有返回 False。

    **用 PostgreSQL 的 advisory lock 而不是 asyncio 锁**：asyncio 锁只在单进程内
    有效，而这个 tick 有三个并发来源——Windows 计划任务、手动的管理端点、
    以及人可能在本地跑的那个 CLI。三者是三个进程，进程内的锁对它们全部无效。

    锁是**会话级**的（`pg_try_advisory_lock`，不是 `_xact_` 那个）：它跨事务存活，
    直到显式解锁或连接关闭。这正是这里需要的——一个 tick 里会开很多个事务，
    锁必须比其中任何一个都长命。

    `try_` 而不是阻塞版：抢不到锁说明另一个 tick 正在跑，**这一轮直接放弃**才是
    正确的行为。排队等下去只会让两个 tick 一前一后各建一个 Run，把当日额度
    加倍用掉，而计划任务下一轮马上又要来了。
    """
    result = await session.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": AUTO_PUBLISH_LOCK_KEY}
    )
    return bool(result.scalar())


async def release_auto_publish_lock(session: AsyncSession) -> None:
    """释放 tick 锁。

    必须在 `finally` 里调。会话级锁虽然会在连接关闭时自动释放，但
    SQLAlchemy 的连接池会让连接**活着回到池子里**——那条连接仍然持有锁，
    而下一个从池里拿到它的人会莫名其妙地拿到一个已经"被占用"的锁。
    结果是：一次没释放的 tick 会让后续所有 tick 永久静默失效。
    """
    await session.execute(
        text("SELECT pg_advisory_unlock(:key)"), {"key": AUTO_PUBLISH_LOCK_KEY}
    )


async def mark_auto_review_acted(session: AsyncSession, *, draft_id: str) -> bool:
    """把一条自动结论标成"真的动过手"。返回是否命中一行。

    `acted` 的语义是**自动审阅者替人发出了这条**，不是"判据通过了"。
    两者必须分开：影子模式下判据通过也不发，若把通过当成 acted，
    那条记录日后看起来就像"这条是自动发布的"，而账号上根本没有这条帖子——
    审计时无法自证。所以只有真的走到 publish 之后才置位。

    命中不到行（返回 False）是可能发生的：tick 崩溃后重放时，
    `insert_auto_review` 会因 UNIQUE(draft_id) 而 DO NOTHING，
    但那时 draft_id 一定已经有行了；真正找不到行只说明有人手工删了它。
    不当成错误，只如实返回。
    """
    stmt = (
        update(AutoReview)
        .where(AutoReview.draft_id == _uuid(draft_id))
        .values(acted=True)
    )
    result = await session.execute(stmt)
    await session.commit()
    return bool(result.rowcount)


async def count_stale_review_claims(session: AsyncSession) -> int:
    """`reap_stale_review_claims` 的**只读**版本，用于转正式前的门槛检查。

    判据完全相同（自动发起的 Run 停在可发布状态却没有 review 行），
    只是不写。门槛检查绝不能顺手把残留收掉——那会让"我检查过了，是干净的"
    这句话变成"我检查的动作把它变干净了"，下一次检查就再也看不到这个问题。
    """
    has_review = (
        select(Review.id)
        .join(Draft, Review.draft_id == Draft.id)
        .where(Draft.agent_run_id == AgentRun.id)
        .exists()
    )
    stmt = (
        select(func.count())
        .select_from(AgentRun)
        .where(
            AgentRun.origin == ORIGIN_SCHEDULED,
            AgentRun.status.in_(PUBLISHABLE_RUN_STATUSES),
            ~has_review,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def list_false_reject_reasons(session: AsyncSession) -> list[list[str]]:
    """自动判"不通过"、人却认为可发的那些条目的**拒绝理由**。

    转正式的门槛要求"每个误拦都能归因到具名判据"（见
    `app/services/auto_review.py::evaluate_go_live_gate`）。只看条数答不了这个问题——
    要看到底是哪条判据在拦，才能判断这个误拦率接不接受（工作单：若误拦集中在
    lexical 层，转正式前直接关掉它）。

    返回每条一组的理由串。空列表意味着那条记录没有留下任何理由——
    那本身就是门槛要抓的东西：一条说不出理由的拒绝是无法归因的。
    """
    stmt = (
        select(AutoReview.reasons)
        .select_from(AutoReview)
        .join(AgentRun, AutoReview.agent_run_id == AgentRun.id)
        .join(Review, Review.draft_id == AutoReview.draft_id)
        .where(
            AgentRun.origin == ORIGIN_SCHEDULED,
            AutoReview.verdict == VERDICT_REJECT,
            Review.status.in_(("approve", "edit")),
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [list(row or []) for row in rows]


async def earliest_auto_review_at(session: AsyncSession) -> datetime | None:
    """第一条自动结论落库的时刻；一条都没有时为 None。

    这是转正式门槛里"观察满 7 天"的起点。刻意取**数据**的最早时刻而不是
    "配置文件最后一次修改时间"——文件的 mtime 会被编辑器、备份还原、git
    checkout 随意改动，而这里要回答的是"这套判据已经在真实输入上跑了多久"，
    那个问题的答案只能从数据里来。
    """
    stmt = select(func.min(AutoReview.created_at))
    return (await session.execute(stmt)).scalar_one_or_none()


async def count_auto_approvals_with_gate_violations(session: AsyncSession) -> int:
    """自动放行、但记录里的内容门禁**非空**的条数。

    正常恒为 0：`evaluate_auto_publish` 在内容门禁非空时一定会判不通过，
    所以"放行 + 门禁非空"这个组合不可能被它写出来。一旦非 0，说明记录与
    判据之间的不变式被破坏了——转正式前的门槛要求它必须是 0，不是因为它
    危险，而是因为它意味着**别的地方有 bug**。

    用 JSONB 路径直接查而不是把行拉回来在 Python 里数：条数无上界，
    而这里只需要一个数。
    """
    stmt = (
        select(func.count())
        .select_from(AutoReview)
        .where(
            AutoReview.verdict == VERDICT_APPROVE,
            AutoReview.checks["content_gate"]["passed"].as_boolean().is_(False),
        )
    )
    return int((await session.execute(stmt)).scalar_one())
