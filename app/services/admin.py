"""运营控制台（Phase 4b）的只读查询服务。

职责边界（刻意画死）：

    HTTP 路由 (app/api/admin.py)
      ↓
    AdminService（本模块）—— 只读、不写库、不调用任何外部 API
      ↓
    app.repositories —— 所有 SQL 只在这里
      ↓
    PostgreSQL

**本模块一行 SQL 都不写**，也**绝不调用 X / LLM**。控制台是观察窗口，不是新的
业务路径：Run 的执行、审核、发布继续全部由 Phase 3/4a 的既有端点和 Graph 负责。

数据来源的两条约定（决定了哪些指标能算、哪些不能）：

1. **发布状态的权威是 `published_post.status`**，不是 `agent_run.status`。
   `agent_run.status` 只是给运维查列表用的镜像。UI 上两者永远分列显示——它们
   可能合法地互相矛盾（发布失败时 Run 仍是 `approved`），合并会把这个事实藏起来。
2. `guardrail_*` / `human_action` / `final_content` **只存在于 LangGraph
   checkpoint**，数据库里没有对应的列。所以：

   - Run 详情可以按条读取（本来就是单条查询的成本）；
   - **聚合类指标一律不做 guardrail 维度**——那意味着把全表 Run 逐个读一遍
     checkpoint，代价不可接受，而且"读不到"与"没通过"会被混成一个数。
     与其猜，不如不做。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.metrics import quality_metrics
from app.models.published_post import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_UNKNOWN,
)
from app.repositories import (
    average_run_duration,
    count_published,
    count_published_grouped_by_status,
    count_review_queue,
    count_reviews_grouped_by_status,
    count_runs,
    count_unresolved_publications,
    daily_published_counts,
    daily_run_counts,
    drafts_by_run,
    get_agent_run,
    get_draft_for_run,
    get_published_post,
    get_review_for_run,
    get_runs_by_ids,
    list_decisions,
    list_published_page,
    list_research_items,
    list_review_queue,
    list_runs_page,
    list_unresolved_publications,
    published_by_run,
    reviews_by_run,
    topic_distribution,
)
from app.schemas import (
    AdminRunDetail,
    AnalyticsResponse,
    AverageRunDuration,
    DecisionTraceRecord,
    DraftRecord,
    MetricAvailability,
    OverviewKpis,
    OverviewResponse,
    Page,
    PublishedListPage,
    PublishedRow,
    PublishOutcomeDistribution,
    PublishRecord,
    ResolutionQueueItem,
    ResearchItemRecord,
    ReviewDecisionDistribution,
    ReviewQueueItem,
    ReviewQueuePage,
    ReviewRecord,
    RunListPage,
    RunSummary,
    TopicCount,
    TrendPoint,
    publish_record_from_row,
)

logger = logging.getLogger(__name__)

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100

# Overview 上预览队列的条数。首页只需要"有多少积压、最急的是哪几条"，
# 完整队列在各自的页面里翻页看。
OVERVIEW_QUEUE_PREVIEW = 10

# 一次请求里最多为多少条待审 Run 读 checkpoint 拿 guardrail 结论。
# 读 checkpoint 是**按条**的数据库往返，不能对无界的结果集做。超过这个数的条目
# guardrail 一律报 None（UI 显示 `—`，表示"本次没读"），绝不猜一个值。
GUARDRAIL_READ_LIMIT = 50

# 列表里的正文预览长度。完整正文在详情页看——列表传全文会让响应体随内容膨胀。
PREVIEW_CHARS = 200

# X 公开指标（impressions / likes / engagement）没有同步机制。
# 这里明确标成不可用，而不是返回 0 假装"表现很差"。
X_PERFORMANCE_UNAVAILABLE_REASON = (
    "X public metrics synchronization has not been implemented."
)

# publish 分发的口径说明。published_post 每个 Run 最多一行、失败重试复用同一行，
# 所以这里统计的是"每个 Run 的当前结局"而不是"尝试次数"——一个失败后重试成功的
# Run 只会留下 published 一行，它失败过的那次不会出现在任何计数里。
PUBLISH_OUTCOME_NOTE = (
    "Counts are per Run (current outcome), not per attempt: published_post holds at "
    "most one row per Run and retries reuse it, so superseded failures are not counted."
)

# 平均耗时的口径说明：只有终态 Run 才写 finished_at。
DURATION_UNAVAILABLE_REASON = (
    "No terminal Run has both started_at and finished_at yet. Only terminal statuses "
    "(published / rejected / failed / completed) record finished_at; approved, edited, "
    "waiting_for_review and unresolved publishes intentionally leave it empty."
)
DURATION_PARTIAL_REASON = (
    "Computed only over terminal Runs (published / rejected / failed / completed). "
    "approved, edited, waiting_for_review and unresolved publishes have no finished_at."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _window_start(days: int, now: datetime | None = None) -> datetime:
    """窗口起点：含今天在内、往前 `days` 个 UTC 自然日的零点。

    `days=7` → 今天零点往前推 6 天。这样图上是 7 根柱子（含今天这个不完整的天），
    而不是 8 根。
    """
    now = now or _now()
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=max(days - 1, 0))


def _daily_series(rows: dict[str, int], days: int, now: datetime | None = None) -> list[TrendPoint]:
    """把稀疏的分桶结果补成连续日期序列。

    没数据的日期必须显式补 0：让前端自己补的话，每个图都要重写一遍同样的日期
    逻辑，而且"缺数据"与"零"会在图上长得一样。
    """
    now = now or _now()
    today = now.astimezone(timezone.utc).date()
    start = _window_start(days, now).date()
    series = []
    cursor = start
    while cursor <= today:
        key = cursor.isoformat()
        series.append(TrendPoint(date=key, count=rows.get(key, 0)))
        cursor += timedelta(days=1)
    return series


def _preview(text: str | None, limit: int = PREVIEW_CHARS) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _page(page: int, page_size: int, total: int) -> Page:
    pages = (total + page_size - 1) // page_size if page_size else 0
    return Page(page=page, page_size=page_size, total=total, pages=pages)


class AdminService:
    """控制台只读查询。

    每次请求构造一个即可（无状态，只持有 session_factory 与 graph 两个引用）。
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], graph) -> None:
        self._session_factory = session_factory
        self._graph = graph

    # ---------- checkpoint：按条读取，失败不致命 ----------

    async def _guardrail(self, run_ids: list[str]) -> dict[str, tuple[bool | None, list[str]]]:
        """按条读 checkpoint 取 guardrail 结论。

        `graph` 可能是测试里的替身，也可能在 checkpoint 尚未写入时返回空 state
        （human_review 刚 interrupt 的瞬间就是这种状态）。任何情况下都退化成
        (None, [])，**不让控制台的读操作因为读不到而不通** —— 显示 `—` 比整个
        页面 500 有用得多。
        """
        targets = run_ids[:GUARDRAIL_READ_LIMIT]
        results: dict[str, tuple[bool | None, list[str]]] = {}

        async def read_one(run_id: str) -> None:
            try:
                snapshot = await self._graph.aget_state({"configurable": {"thread_id": run_id}})
                values = getattr(snapshot, "values", None) or {}
                results[run_id] = (
                    values.get("guardrail_passed"),
                    list(values.get("guardrail_reasons") or []),
                )
            except Exception:  # noqa: BLE001 - 读不到就是读不到，不该影响列表
                logger.warning("guardrail state unavailable (run_id=%s)", run_id, exc_info=True)
                results[run_id] = (None, [])

        if targets:
            await asyncio.gather(*(read_one(run_id) for run_id in targets))
        for run_id in run_ids:
            results.setdefault(run_id, (None, []))
        return results

    # ---------- 列表行的组装 ----------

    def _summaries(self, runs, reviews: dict, published: dict) -> list[RunSummary]:
        return [
            RunSummary(
                run_id=str(run.id),
                topic=run.topic,
                goal=run.goal,
                run_status=run.status,
                review_status=getattr(reviews.get(run.id), "status", None),
                publish_status=getattr(published.get(run.id), "status", None),
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error=run.error,
            )
            for run in runs
        ]

    async def _queue_items(self, runs) -> list[ReviewQueueItem]:
        """待审 Run → 队列条目。草稿正文来自 draft 表，guardrail 来自 checkpoint。"""
        run_ids = [run.id for run in runs]
        async with self._session_factory() as session:
            drafts = await drafts_by_run(session, run_ids)
            reviews = await reviews_by_run(session, run_ids)
        guardrails = await self._guardrail([str(run.id) for run in runs])

        items = []
        for run in runs:
            draft = drafts.get(run.id)
            passed, reasons = guardrails.get(str(run.id), (None, []))
            # 已经审过的 Run 一旦被复核（例如 resume 失败后状态回退），
            # 草稿正文要用**待审的原文**，不能拿 review 里的人工稿冒充。
            decided = reviews.get(run.id)
            content = draft.content if draft is not None else None
            if content is None and decided is not None:
                content = decided.edited_content or decided.original_content
            items.append(
                ReviewQueueItem(
                    run_id=str(run.id),
                    topic=run.topic,
                    run_status=run.status,
                    created_at=run.created_at,
                    draft_id=str(draft.id) if draft is not None else None,
                    draft_preview=_preview(content),
                    guardrail_passed=passed,
                    guardrail_reasons=reasons,
                )
            )
        return items

    # ---------- Overview ----------

    async def overview(self, *, days: int) -> OverviewResponse:
        window_start = _window_start(days)
        async with self._session_factory() as session:
            publish_status_counts = await count_published_grouped_by_status(session)
            daily_runs = await daily_run_counts(session, date_from=window_start)
            daily_published = await daily_published_counts(session, date_from=window_start)
            total_runs = await count_runs(session)
            review_queue_total = await count_review_queue(session)
            queue_runs = await list_review_queue(session, limit=OVERVIEW_QUEUE_PREVIEW, offset=0)
            resolution_total = await count_unresolved_publications(session)
            unresolved = await list_unresolved_publications(
                session, limit=OVERVIEW_QUEUE_PREVIEW
            )
            unresolved_runs = await get_runs_by_ids(session, [row.agent_run_id for row in unresolved])

        queue_items = await self._queue_items(queue_runs)

        return OverviewResponse(
            window_days=days,
            kpis=OverviewKpis(
                total_runs=total_runs,
                waiting_for_review=review_queue_total,
                # 三项一律取自 published_post —— 发布状态的权威。
                published=publish_status_counts.get(STATUS_PUBLISHED, 0),
                publish_failed=publish_status_counts.get(STATUS_FAILED, 0),
                publish_unknown=publish_status_counts.get(STATUS_UNKNOWN, 0),
                publishing=publish_status_counts.get(STATUS_PUBLISHING, 0),
            ),
            runs_trend=_daily_series(daily_runs, days),
            published_trend=_daily_series(daily_published, days),
            review_queue=queue_items,
            review_queue_total=review_queue_total,
            resolution_queue=[
                ResolutionQueueItem(
                    run_id=str(row.agent_run_id),
                    topic=getattr(unresolved_runs.get(row.agent_run_id), "topic", None),
                    run_status=getattr(unresolved_runs.get(row.agent_run_id), "status", None),
                    publish=publish_record_from_row(row),
                    detail=row.last_error,
                )
                for row in unresolved
            ],
            resolution_queue_total=resolution_total,
        )

    # ---------- Runs ----------

    async def list_runs(
        self,
        *,
        status: str | None,
        topic: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        page: int,
        page_size: int,
    ) -> RunListPage:
        async with self._session_factory() as session:
            total = await count_runs(
                session, status=status, topic=topic, date_from=date_from, date_to=date_to
            )
            runs = await list_runs_page(
                session,
                status=status,
                topic=topic,
                date_from=date_from,
                date_to=date_to,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            run_ids = [run.id for run in runs]
            # 审核与发布状态用**批量**查询补齐，避免每行两次往返（N+1）。
            reviews = await reviews_by_run(session, run_ids)
            published = await published_by_run(session, run_ids)

        return RunListPage(
            items=self._summaries(runs, reviews, published),
            page=_page(page, page_size, total),
        )

    async def run_detail(self, run_id: str) -> AdminRunDetail | None:
        async with self._session_factory() as session:
            run = await get_agent_run(session, run_id)
            if run is None:
                return None
            draft = await get_draft_for_run(session, run_id)
            review = await get_review_for_run(session, run_id)
            published = await get_published_post(session, run_id)
            items = await list_research_items(session, run_id)
            # 决策轨迹来自数据库而不是 checkpoint：Run 的每一步"为什么这么做"
            # 必须比 checkpoint 活得久，否则清理过 checkpoint 的 Run 在控制台上
            # 就只剩一堆没有来由的产物。
            decisions = await list_decisions(session, run_id)

        # checkpoint 只读，用于补齐数据库里没有的那几个字段。
        snapshot = await self._graph.aget_state({"configurable": {"thread_id": run_id}})
        state = getattr(snapshot, "values", None) or {}

        return AdminRunDetail(
            run_id=str(run.id),
            topic=run.topic,
            goal=run.goal,
            run_status=run.status,
            review_status=getattr(review, "status", None),
            publish_status=getattr(published, "status", None),
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error=run.error,
            guardrail_passed=state.get("guardrail_passed"),
            guardrail_reasons=list(state.get("guardrail_reasons") or []),
            human_action=state.get("human_action"),
            final_content=state.get("final_content"),
            research_items=[
                ResearchItemRecord(
                    item_id=str(item.id),
                    platform_post_id=item.platform_post_id,
                    author_username=item.author_username,
                    content=item.content,
                    source_url=item.source_url,
                    likes=item.likes,
                    reposts=item.reposts,
                    replies=item.replies,
                    quote_count=item.quote_count,
                    published_at=item.published_at,
                    # 原样透出，**不拿 created_at 兜底**：老数据这一列为 NULL，
                    # 那正是"我们不知道什么时候观察的"。顶上会让一条三天前的
                    # 讨论看起来刚刚被抓取。
                    observed_at=item.observed_at,
                    created_at=item.created_at,
                )
                for item in items
            ],
            decision_trace=[
                DecisionTraceRecord(
                    step=row.step,
                    action=row.action,
                    reason=row.reason,
                    target=row.target,
                    input_summary=row.input_summary,
                    result_summary=row.result_summary,
                    created_at=row.created_at,
                )
                for row in decisions
            ],
            # 质量指标只对 Agent 运行有意义：topic 链路的 Run 没有 planner 循环，
            # 那几个计数恒为 0，报出去只会让人以为"这一轮什么都没做"。
            #
            # 停在 human_review 的 Run 照样算：Agent 循环那时已经跑完，
            # step_count / critic_tally / 决策轨迹都已经是终值。
            quality=quality_metrics(state) if state.get("goal") else None,
            draft=(
                DraftRecord(
                    draft_id=str(draft.id),
                    content=draft.content,
                    status=draft.status,
                    version=draft.version,
                    model_name=draft.model_name,
                    prompt_version=draft.prompt_version,
                    generated_at=draft.generated_at,
                )
                if draft is not None
                else None
            ),
            review=(
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
            ),
            publish=publish_record_from_row(published) if published is not None else None,
        )

    # ---------- Review 队列 ----------

    async def review_queue(
        self, *, page: int, page_size: int, content_max_length: int
    ) -> ReviewQueuePage:
        """`content_max_length` 由**调用方**（HTTP 层从已加载的 runtime.persona 取）
        传进来，而不是在这里现读配置：本服务只碰数据库，配置不归它管，每次请求
        重读一遍 YAML 也没有道理。字段本身是必填的——没有一个"未设置"的合法状态。
        """
        async with self._session_factory() as session:
            total = await count_review_queue(session)
            runs = await list_review_queue(
                session, limit=page_size, offset=(page - 1) * page_size
            )
        return ReviewQueuePage(
            items=await self._queue_items(runs),
            page=_page(page, page_size, total),
            content_max_length=content_max_length,
        )

    # ---------- Published ----------

    async def list_published(
        self,
        *,
        status: str | None,
        unresolved: bool,
        date_from: datetime | None,
        date_to: datetime | None,
        page: int,
        page_size: int,
    ) -> PublishedListPage:
        async with self._session_factory() as session:
            total = await count_published(
                session, status=status, unresolved=unresolved, date_from=date_from, date_to=date_to
            )
            rows = await list_published_page(
                session,
                status=status,
                unresolved=unresolved,
                date_from=date_from,
                date_to=date_to,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            runs = await get_runs_by_ids(session, [row.agent_run_id for row in rows])

        return PublishedListPage(
            items=[
                PublishedRow(
                    publish_id=str(row.id),
                    run_id=str(row.agent_run_id),
                    draft_id=str(row.draft_id),
                    topic=getattr(runs.get(row.agent_run_id), "topic", None),
                    body=row.body,
                    status=row.status,
                    x_post_id=row.x_post_id,
                    published_at=row.published_at,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    last_error=row.last_error,
                )
                for row in rows
            ],
            page=_page(page, page_size, total),
        )

    # ---------- Analytics ----------

    async def analytics(self, *, days: int) -> AnalyticsResponse:
        window_start = _window_start(days)
        async with self._session_factory() as session:
            publish_counts = await count_published_grouped_by_status(session)
            review_counts = await count_reviews_grouped_by_status(session)
            daily_runs = await daily_run_counts(session, date_from=window_start)
            daily_published = await daily_published_counts(session, date_from=window_start)
            run_volume = await count_runs(session, date_from=window_start)
            topics = await topic_distribution(session, limit=10)
            average, sampled, total_runs = await average_run_duration(session)

        return AnalyticsResponse(
            window_days=days,
            run_volume=run_volume,
            review_decisions=ReviewDecisionDistribution(
                approve=review_counts.get("approve", 0),
                edit=review_counts.get("edit", 0),
                reject=review_counts.get("reject", 0),
                # pending 是 Draft 的行状态而非人工决定，正常流程下不会出现在 review 表；
                # 归入 other，既不让它冒充某个决定，也不静默丢掉。
                other=review_counts.get("pending", 0)
                + sum(
                    count
                    for status, count in review_counts.items()
                    if status not in {"approve", "edit", "reject", "pending"}
                ),
            ),
            publish_outcomes=PublishOutcomeDistribution(
                published=publish_counts.get(STATUS_PUBLISHED, 0),
                failed=publish_counts.get(STATUS_FAILED, 0),
                publish_unknown=publish_counts.get(STATUS_UNKNOWN, 0),
                publishing=publish_counts.get(STATUS_PUBLISHING, 0),
                note=PUBLISH_OUTCOME_NOTE,
            ),
            daily_runs=_daily_series(daily_runs, days),
            daily_published=_daily_series(daily_published, days),
            average_run_duration=AverageRunDuration(
                available=average is not None and sampled > 0,
                average_seconds=average,
                sampled_runs=sampled,
                total_runs=total_runs,
                reason=(
                    None
                    if average is not None and sampled > 0
                    else DURATION_UNAVAILABLE_REASON
                ),
            ),
            topic_distribution=[TopicCount(topic=topic, count=count) for topic, count in topics],
            x_content_performance=MetricAvailability(
                available=False, reason=X_PERFORMANCE_UNAVAILABLE_REASON
            ),
        )
