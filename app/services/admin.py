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
from typing import Any, Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.labels import claim_scope_label, viewpoint_stance_label
from app.agent.metrics import quality_metrics
from app.feedback.runtime import FEEDBACK_SNAPSHOT_READ_LIMIT, load_feedback_report
from app.feedback.schema import FeedbackContext
from app.learning.proposal import TARGET_LABELS, generate_proposals, proposal_id, validate_proposal
from app.learning.runtime import load_learning_report
from app.learning.schema import LearningProposal, LearningReport, ProposalStatus
from app.models.learning_proposal import LearningProposalRecord
from app.style.recipe import recipe_version
from app.style.runtime import resolve_recipe
from app.models.published_post import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_UNKNOWN,
)
from app.repositories import (
    average_run_duration,
    count_post_metrics_snapshots,
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
    insert_learning_proposal_decision,
    latest_metrics_by_post,
    list_decisions,
    list_learning_proposal_decisions,
    list_published_page,
    list_post_metrics_snapshots,
    list_published_posts_with_x_id,
    list_recent_post_metrics_snapshots,
    list_research_items,
    list_review_queue,
    list_runs_page,
    list_unresolved_publications,
    published_by_run,
    auto_reviews_by_run,
    reviews_by_run,
    topic_distribution,
)
from app.schemas import (
    AdminRunDetail,
    AnalyticsResponse,
    AverageRunDuration,
    DecisionTraceRecord,
    DraftRecord,
    FeedbackOverview,
    FeedbackPostSummary,
    FeedbackSignalRow,
    LearningCohortRow,
    LearningComparisonRow,
    LearningEvidenceRow,
    LearningOverview,
    LearningProposalRow,
    LearningProposalValidationRow,
    LearningReadinessRow,
    MetricAvailability,
    OverviewKpis,
    OverviewResponse,
    Page,
    PublishedListPage,
    PublishedRow,
    PublishOutcomeDistribution,
    PublishRecord,
    PROPOSAL_ACTION_STATUS,
    ProposalsOverview,
    ResolutionQueueItem,
    ResearchItemRecord,
    ReviewDecisionDistribution,
    ReviewQueueItem,
    ReviewQueuePage,
    ReviewRecord,
    RunAutoReviewSummary,
    RunFeedbackSummary,
    RunListPage,
    RunStyleSummary,
    RunSummary,
    RunViewpointSummary,
    TopicCount,
    TrendPoint,
    ViewpointCandidateBrief,
    ViewpointEvidenceItem,
    publish_record_from_row,
)
from app.style.labels import (
    density_label,
    ending_label,
    evidence_label,
    hook_label,
    label,
    stance_label,
    structure_label,
    tone_label,
)
from app.style.signals import ai_style_signals

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
#
# Phase 5B.4 起这句话只在**一条快照都没有**时成立：采集脚本
# `scripts/refresh_post_metrics.py` 已经实现（手工运行，没有调度器）。
# 有快照时改用下面的 SCOPE_NOTE——"没实现"会变成一句假话。
X_PERFORMANCE_UNAVAILABLE_REASON = (
    "No post metrics have been collected yet; run scripts/refresh_post_metrics.py."
)

#: 有快照时的口径说明。**必须**说清范围，否则读者会以为这是 X 的完整表现指标。
X_PERFORMANCE_AVAILABLE_NOTE = (
    "Collected from X public counts (likes/replies/reposts/quotes) at manual "
    "observation times; impressions and bookmarks are not collected, and these "
    "numbers are observations, not quality scores."
)

#: 反馈一览里最多列多少条帖子。同 GUARDRAIL_READ_LIMIT：一目览必须有上界。
FEEDBACK_POSTS_LIMIT = 50

#: 学习准备度一览的口径说明（Phase 5C）。**必须**说清两件事：
#: 门槛是采集量而不是显著性；"可以作为学习材料"不等于"立即改 Agent"。
LEARNING_AVAILABLE_NOTE = (
    "Learning readiness compares cohorts of already-published posts at one fixed "
    "observation window; the sample threshold is a collection-volume gate, not a "
    "statistical significance test, and every comparison is an observation, never a "
    "rule. learning_ready=true only means the feedback may serve as future learning "
    "material; this build never modifies the Agent."
)

#: 一条已发布内容都读不到时的原因。返回原因，**不返回一串 0**。
LEARNING_UNAVAILABLE_REASON = (
    "No published post with an x_post_id and published_at exists yet, so there is "
    "nothing to observe and no record, cohort or comparison was produced."
)

#: 提案一览的口径说明（Phase 5D）。**必须**说清三件事：提案是观察性比较推出来的、
#: 每条都引用证据、批准只是记一次裁决而本阶段没有任何应用路径。
PROPOSALS_AVAILABLE_NOTE = (
    "Each proposal is derived from cohort comparisons in the learning report at one "
    "fixed observation window, cites the evidence ids it rests on, and carries no "
    "causal claim. Approving one records a human decision for a later phase only: "
    "this build never modifies prompts, StyleRecipe, weights, thresholds or budget."
)

#: 闸门关着时的原因。**零条提案是一个结论，不是一次失败**（工作单第七节）。
PROPOSALS_GATE_CLOSED_REASON = (
    "learning_ready is false, so zero proposals are produced. The readiness gate is "
    "a hard requirement rather than a ranking: insufficient data yields no suggestion "
    "at all instead of a few plausible-looking ones."
)

#: 审核提案这条路径的错误码。**不带 HTTP 状态**：服务层不认识 HTTP，
#: 由 app/api/admin.py 把 reason 映射成状态码。
PROPOSAL_REVIEW_NOT_FOUND = "not_found"
PROPOSAL_REVIEW_ALREADY_DECIDED = "already_reviewed"


class LearningProposalReviewError(Exception):
    """"这条提案现在不能裁决"——分两种，原因必须能区分。

    - `not_found`：这条提案**在当前观察下不存在**。可能 id 写错了，也可能是
      它依据的那条比较已经不成立（数据变了、窗口变了）。两种情形在处置上是
      同一件事：现在拿不到这条提案，因此无从裁决。**绝不能**拿库里存过的
      内容顶上——那会批准一条已经不成立的建议。
    - `already_reviewed`：这条提案已经被裁决过。裁决是不可改写的审计记录，
      第二次写入被唯一键挡住（同 post_metrics_snapshot 的幂等口径）。
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


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


def _auto_review_summary(row) -> RunAutoReviewSummary | None:
    """`auto_review` 行 → 控制台用的摘要。没有行就是 None。

    None 与"有行但判据未通过"是两件事，界面必须分得开：前者是**人自己发起的
    Run**（或自动 Run 还没跑到判据那一步），后者是机器明确把它留给了人。
    用 None 之外的任何值兜底都会让这两件事看起来一样。
    """
    if row is None:
        return None
    return RunAutoReviewSummary(
        mode=row.mode,
        verdict=row.verdict,
        reasons=list(row.reasons or []),
        critic_score=row.critic_score,
        acted=row.acted,
    )


def _page(page: int, page_size: int, total: int) -> Page:
    pages = (total + page_size - 1) // page_size if page_size else 0
    return Page(page=page, page_size=page_size, total=total, pages=pages)


def style_summary(state: Mapping[str, Any]) -> RunStyleSummary | None:
    """从 checkpoint 的 `style_recipe` 拼出 Run Detail 的写作结构块。

    ## 为什么没有 recipe 就返回 None

    `resolve_recipe` 在**没有 Profile 时也会给一个中性 recipe**（这是刻意的：
    Writer 那里永远有个明确的东西在约束它，而不是一片空白）。所以"state 里
    没有 style_recipe"这个分支只剩两种可能：

    1. 这不是 Agent 运行（V1 topic 链）——它的 prompt 里根本没有 `{{STYLE}}`；
    2. 这个 Run 跑在 Phase 5B.2 之前，checkpoint 里从来没有这个键。

    两种情况下界面都应当什么也不显示，而不是画一个"结构：不指定结构"的块——
    那会让人以为 Agent 主动选了中性结构，而实际上它那会儿还没有这个能力。

    （调用点另外还按 `goal` 门控一次，见 `run_detail`。）

    ## 为什么在这里重算 AI 味信号

    信号是正文的纯函数，重算逐位相同。存的话就多一个会漂移的来源，
    而且风格参数一改，存下来的旧信号会与新口径对不上却毫无提示。

    ## 量的是哪一份正文：`final_content or draft_content`

    `final_content` **只在人工审核之后才有**（`nodes/human_review.py` 在
    approve / edit 时才写它）。若只认它，那么**停在待审队列里的每一个 Run**
    都会显示"没有交付正文，因此没有可计算的痕迹"——而待审的 Run 恰恰是
    最需要看这个块的地方。所以在人还没决定时退到 `draft_content`：
    那一刻 Agent 交付出来、正被人评判的东西就是它。
    """
    recipe = state.get("style_recipe")
    if recipe is None:
        return None

    content = state.get("final_content") or state.get("draft_content")
    return RunStyleSummary(
        structure_type=label(recipe.structure_type),
        structure_label=structure_label(recipe.structure_type),
        hook_type=label(recipe.hook_type),
        hook_label=hook_label(recipe.hook_type),
        observed_target_length=recipe.observed_target_length,
        effective_target_length=recipe.effective_target_length,
        hard_max_length=recipe.hard_max_length,
        density=label(recipe.density),
        density_label=density_label(recipe.density),
        stance_strength=label(recipe.stance_strength),
        stance_label=stance_label(recipe.stance_strength),
        evidence_requirement=label(recipe.evidence_requirement),
        evidence_label=evidence_label(recipe.evidence_requirement),
        tone=label(recipe.tone),
        tone_label=tone_label(recipe.tone),
        ending_type=label(recipe.ending_type),
        ending_label=ending_label(recipe.ending_type),
        reasons=list(recipe.reasons),
        profile_ref=recipe.profile_ref,
        # 没有交付正文就没有可测的对象。这里给 None 而不是空信号：
        # 空信号会被读成"测过了，一个痕迹都没有"，那是另一个结论。
        ai_signals=ai_style_signals(content) if content else None,
    )


def _evidence_items(fact_ids, pack) -> list[ViewpointEvidenceItem]:
    """把 fact_id 解析回事实原文。

    ## 为什么"解析不到就跳过"而不是报错或留一个空壳

    悬空引用是 RULE-006 要抓的东西，**不是**界面该崩的东西：Run Detail 必须
    能打开一个坏掉的 Run——那恰恰是它最需要被打开的时候。所以这里跳过解析
    不出来的 id，但**不伪造**一条"（找不到）"的占位事实：那会让人以为证据包
    里真有这么一条。少一条看得见（前端会显示条数），编一条看不见。

    只看**同一个 state 的**证据包：观点引的 fact 必须来自本 Run 的证据包，
    跨 Run 去找等于承认这条链断了。
    """
    by_id = {
        fact.fact_id: fact.fact
        for fact in (getattr(pack, "facts", None) or [])
        if getattr(fact, "fact_id", "")
    }
    return [
        ViewpointEvidenceItem(fact_id=str(fid), text=by_id[str(fid)])
        for fid in (fact_ids or [])
        if str(fid) in by_id
    ]


def viewpoint_summary(state: Mapping[str, Any]) -> RunViewpointSummary | None:
    """从 checkpoint 的 `selected_angle` 拼出 Run Detail 的核心判断块。

    ## 为什么没有观点就返回 None

    与 `style_summary` 同一条：`selected_angle` 缺席只剩两种可能——这不是
    Agent 运行（V1 topic 链根本没有这一步），或者这个 Run 停在还没选观点就
    结束的位置。两种情况下界面都应当什么也不显示，而不是画一个空白的
    "核心判断"块——那会让人以为 Agent 形成了判断而内容丢了。
    （调用点另外还按 `goal` 门控一次，见 `run_detail`。）

    ## 为什么 `other_candidates` 里排除了被选中的那条

    把选中的那条再列一遍，界面会长出两个看起来等价、实则一个是决策一个是
    备选的"核心判断"。按 `angle_id` 排除，而不是按 claim 文本比——同一个
    判断在重生成之后可能换了 id，但**同一个 id 在这一次运行里只会指一条**。
    """
    angle = state.get("selected_angle")
    if angle is None:
        return None

    pack = state.get("evidence_pack")
    assessment = getattr(angle, "assessment", None)
    stance = getattr(angle, "stance", None)
    max_stance = getattr(angle, "max_stance", None)
    declared = getattr(angle, "stance_declared", None)

    others = [
        ViewpointCandidateBrief(
            claim=other.claim,
            viewpoint_score=other.viewpoint_score,
            stance=str(getattr(getattr(other, "stance", None), "value", "")),
            stance_label=viewpoint_stance_label(getattr(other, "stance", None))
            if getattr(other, "stance", None)
            else "",
        )
        for other in (state.get("angles") or [])
        if getattr(other, "angle_id", None) != getattr(angle, "angle_id", None)
    ]

    return RunViewpointSummary(
        claim=angle.claim,
        why_it_matters=angle.why_it_matters,
        reasoning=getattr(angle, "reasoning", ""),
        evidence=_evidence_items(angle.evidence_ids, pack),
        counterpoints=list(getattr(angle, "counterpoints", None) or []),
        counterpoint_evidence=_evidence_items(
            getattr(angle, "counterpoint_evidence_ids", None), pack
        ),
        assumptions=list(getattr(angle, "assumptions", None) or []),
        implications=list(getattr(angle, "implications", None) or []),
        risk=getattr(angle, "risk", ""),
        confidence=angle.confidence,
        viewpoint_score=getattr(angle, "viewpoint_score", None),
        stance=str(getattr(stance, "value", "")),
        stance_label=viewpoint_stance_label(stance) if stance else "",
        # 只在**与生效值不同**时回传申报值：相同时多显示一个字段，会让人以为
        # 那两个数永远是两件事，而"模型申报了 strong、证据也支持 strong"时
        # 它们本来就是一回事。
        stance_declared=str(getattr(declared, "value", "")) if declared else None,
        max_stance=str(getattr(max_stance, "value", "")),
        max_stance_label=viewpoint_stance_label(max_stance) if max_stance else "",
        claim_scope=str(getattr(getattr(angle, "claim_scope", None), "value", "")) or None,
        claim_scope_label=claim_scope_label(angle.claim_scope) if angle.claim_scope else "",
        evidence_scope=str(getattr(getattr(angle, "evidence_scope", None), "value", ""))
        or None,
        evidence_scope_label=claim_scope_label(angle.evidence_scope)
        if angle.evidence_scope
        else "",
        scope_exceeds_evidence=getattr(angle, "scope_exceeds_evidence", None),
        scope_declared_missing=getattr(angle, "scope_declared_missing", None),
        assessment=assessment,
        other_candidates=others,
    )


def _feedback_post_summaries(
    rows: list[Any], posts_by_id: Mapping[str, Any]
) -> list[FeedbackPostSummary]:
    """观察行 → 每帖一条"最新一次观察"摘要，按最近观察时刻降序，取前 N 条。

    取**最新**那一条而不是求和/取平均：界面要回答的是"这条帖子现在怎么样"，
    而把不同时刻的观察加起来会造出一个任何时刻都不曾成立的数。
    完整的观察序列在归因里（`app/feedback/profile.py`）和原始快照表里。

    计数**原样搬运，包括 None**。这里没有任何 `or 0`：`None` 是"这次没读到"，
    `0` 是"读到的是零"，界面必须能分开显示。
    """
    by_post: dict[Any, list[Any]] = {}
    for row in rows:
        by_post.setdefault(row.published_post_id, []).append(row)

    summaries = []
    for group in by_post.values():
        latest = max(group, key=lambda r: (r.observed_at, str(r.id)))
        post = posts_by_id.get(str(latest.published_post_id))
        summaries.append(
            FeedbackPostSummary(
                published_post_id=str(latest.published_post_id),
                x_post_id=latest.x_post_id,
                # 发布时刻来自 published_post（来源给定的事实），不是从快照推的。
                # 查不到那行就是 None——不拿 observed_at 冒充。
                published_at=post.published_at if post is not None else None,
                observation_count=len(group),
                latest_observed_at=latest.observed_at,
                hours_since_publish=latest.hours_since_publish,
                likes=latest.likes,
                replies=latest.replies,
                reposts=latest.reposts,
                quotes=latest.quotes,
                impressions=latest.impressions,
                bookmarks=latest.bookmarks,
            )
        )
    summaries.sort(
        key=lambda s: s.latest_observed_at or _EPOCH, reverse=True
    )
    return summaries[:FEEDBACK_POSTS_LIMIT]


#: 排序哨兵：没有观察时刻的摘要排在最后（而不是因为 `None` 不可比较而报错）。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


#: `feedback_context` 这个键**不在** checkpoint 里时的原因。两种"没有反馈"
#: 必须分开说：键缺席是"那次运行根本没读过反馈"（Phase 5B.4 之前的运行），
#: 而记住一个空的上下文是"读过了，当时确实没有观察数据"。前者是版本问题，
#: 后者是数据问题，处置完全不同。
FEEDBACK_CONTEXT_ABSENT_REASON = (
    "This Run did not read any feedback context. Runs from Phase 5B.4 and earlier "
    "never had one; a later Run stores an empty context instead of omitting the key."
)

#: 键在、但内容是空的（本次运行时还没有任何观察）。
FEEDBACK_CONTEXT_EMPTY_REASON = (
    "No post metrics had been collected when this Run started, so it read an empty "
    "feedback context. Run scripts/refresh_post_metrics.py and a later Run will see data."
)


def feedback_summary(state: Mapping[str, Any]) -> RunFeedbackSummary:
    """checkpoint 的 `feedback_context` → **这次运行当时读到了什么**。

    刻意**不**拿现在的快照现算一份塞进来：快照在这之后还会继续增加，而那次运行
    看到的是当时的那一批。用最新的数据顶替，会让 Run Detail 显示一个这次运行
    根本没见过的样本量。

    两种"没有"分开：键缺席 vs 键在但为空（见上面两条原因说明）。
    **都不返回一串 0**——0 会被读成"读到了，结果是零互动"。
    """
    raw = state.get("feedback_context")
    if raw is None:
        return RunFeedbackSummary(available=False, reason=FEEDBACK_CONTEXT_ABSENT_REASON)

    context = raw if isinstance(raw, FeedbackContext) else FeedbackContext.model_validate(raw)
    if context.is_empty:
        return RunFeedbackSummary(available=False, reason=FEEDBACK_CONTEXT_EMPTY_REASON)

    return RunFeedbackSummary(
        available=True,
        sample_count=context.sample_count,
        post_count=context.post_count,
        snapshot_count=context.snapshot_count,
        latest_observed_at=context.latest_observed_at,
        signals=[
            FeedbackSignalRow(
                kind=signal.kind.value,
                statement=signal.statement,
                metric=signal.metric,
                value=signal.value,
                comparison_group=signal.comparison_group,
                sample_count=signal.sample_count,
                confidence=signal.confidence,
                evidence=list(signal.evidence),
                window=signal.window,
            )
            for signal in context.signals
        ],
        limitations=list(context.limitations),
        source_snapshot_ids=list(context.source_snapshot_ids),
    )


def observation_summary(
    rows: list[Any], published: Any, *, observation_count: int | None = None
) -> FeedbackPostSummary | None:
    """一个 Run 的已发布帖子的**最新一次观察**；一条快照都没有时是 `None`。

    刻意不返回一行 0：`published_post` 存在但从未采集过时，界面应当显示
    "还没有观察"，而不是"点赞 0 次"——后者是一个会被当成结论的数字。

    与 `_feedback_post_summaries` 共用同一条口径（取最新的一条，计数原样搬运
    包括 `None`），区别只在这里只处理一条帖子的观察序列。

    `observation_count` 可以单独传：发布列表一次取一页帖子的最新快照 + 计数
    （`latest_metrics_by_post`），那条路径上手里只有**一条**最新的行，
    `len(rows)` 会是 1，而这个帖子其实被观察过 N 次。默认 `None` = 用 `len(rows)`，
    即调用方确实把整条序列都拿来了。
    """
    if not rows:
        return None
    latest = max(rows, key=lambda row: (row.observed_at, str(row.id)))
    return FeedbackPostSummary(
        published_post_id=str(latest.published_post_id),
        x_post_id=latest.x_post_id,
        published_at=getattr(published, "published_at", None),
        observation_count=len(rows) if observation_count is None else observation_count,
        latest_observed_at=latest.observed_at,
        hours_since_publish=latest.hours_since_publish,
        likes=latest.likes,
        replies=latest.replies,
        reposts=latest.reposts,
        quotes=latest.quotes,
        impressions=latest.impressions,
        bookmarks=latest.bookmarks,
    )


#: 一条裁决在当前观察下**不再被生成**时的说明。措辞刻意说清"下面显示的是当时的
#: 内容"：不这么说，读者会以为这是刚算出来的提案。
PROPOSAL_STALE_REASON = (
    "This proposal is no longer produced by the current observation (the window's data "
    "changed, or the comparison it rested on no longer holds). What is shown below is "
    "the snapshot recorded at review time, not a freshly derived proposal."
)

#: 批准过的提案在当前配方已经不是当时那一版时的说明（工作单第三十二节）。
PROPOSAL_EXPIRED_REASON = (
    "The approved recipe version is no longer the current one (current_recipe_version "
    "differs from the live recipe). The approval still stands as a record of what a "
    "human decided at that time; it is shown as expired so it is not mistaken for a "
    "decision that applies to the current recipe."
)


def _proposal_row(
    proposal: LearningProposal,
    record: LearningProposalRecord | None,
    *,
    live_recipe_version: str | None,
    stale_reason: str | None = None,
) -> LearningProposalRow:
    """一条**现算出来的**提案 + 它可能有的裁决 → 一行响应。

    `record` 为 `None`（没人裁决过）时状态就是 `proposed`，而且只有这一种情况
    `reviewable=True`——裁决过的提案不再可裁决，未裁决但已不再生成的历史裁决
    也没有对应的现算提案可以裁决。
    """
    status, expired_reason = (
        _effective_status(record, live_recipe_version=live_recipe_version)
        if record is not None
        else (ProposalStatus.PROPOSED.value, None)
    )
    validation = validate_proposal(proposal)
    return LearningProposalRow(
        id=proposal.id,
        target=proposal.target,
        target_label=TARGET_LABELS.get(proposal.target, proposal.target),
        current_value=proposal.current_value,
        proposed_value=proposal.proposed_value,
        rationale=proposal.rationale,
        evidence_ids=list(proposal.evidence_ids),
        sample_count=proposal.sample_count,
        observation_window=proposal.observation_window,
        comparison_group=proposal.comparison_group,
        # 恒为 None（没有校准过的概率模型）。原样搬运，绝不在这里补一个数。
        confidence=proposal.confidence,
        limitations=[*proposal.limitations, *([expired_reason] if expired_reason else [])],
        status=status,
        current_recipe_version=proposal.current_recipe_version,
        proposed_recipe_version=proposal.proposed_recipe_version,
        outlier_count=proposal.outlier_count,
        automatic_application=proposal.automatic_application,
        validation=LearningProposalValidationRow(
            valid=validation.valid,
            reasons=list(validation.reasons),
            passed_checks=list(validation.passed_checks),
        ),
        reviewable=record is None,
        reviewed_at=record.reviewed_at if record is not None else None,
        review_note=record.note if record is not None else None,
        stale_reason=stale_reason,
    )


def _proposal_row_from_record(
    record: LearningProposalRecord, *, live_recipe_version: str | None
) -> LearningProposalRow:
    """一条**只剩裁决**的提案（现在算不出来了）→ 一行响应。

    内容全部来自库里那一行，**一个字段都不现算**：现算出来的取值属于另一条提案，
    混进来就会变成"显示的是 A、依据的是 B"。`validation` 给空结果并说明理由——
    没有现算的对象，就没有可校验的东西，编一份看起来通过的校验结果是更糟的选择。
    """
    status, expired_reason = _effective_status(record, live_recipe_version=live_recipe_version)
    return LearningProposalRow(
        id=record.proposal_key,
        target=record.target,
        target_label=TARGET_LABELS.get(record.target, record.target),
        current_value=record.current_value,
        proposed_value=record.proposed_value,
        rationale=record.rationale,
        evidence_ids=list(record.evidence_ids),
        sample_count=record.sample_count,
        observation_window=record.observation_window,
        comparison_group=None,
        confidence=None,
        limitations=[
            *record.limitations,
            PROPOSAL_STALE_REASON,
            *([expired_reason] if expired_reason else []),
        ],
        status=status,
        current_recipe_version=record.current_recipe_version,
        proposed_recipe_version=record.proposed_recipe_version,
        outlier_count=0,
        automatic_application=False,
        validation=LearningProposalValidationRow(
            valid=False,
            reasons=[
                "这条提案在当前观察下不再被生成，因此没有可校验的现算对象；"
                "上面显示的是裁决时的快照。"
            ],
            passed_checks=[],
        ),
        reviewable=False,
        reviewed_at=record.reviewed_at,
        review_note=record.note,
        stale_reason=PROPOSAL_STALE_REASON,
    )


def _merge_proposals(
    proposals: Sequence[LearningProposal],
    records: Sequence[LearningProposalRecord],
    *,
    live_recipe_version: str | None,
) -> list[LearningProposalRow]:
    """现算的提案 ∪ 已存的裁决，按**现算顺序在前、历史裁决在后**排列。

    顺序刻意稳定：现算那一段的顺序由 `generate_proposals` 的固定维度顺序决定，
    历史那一段按 `reviewed_at` 正序（库查询已排好）。不按状态或时间重排——
    那会引入一个没有依据的优先级，而"先看哪条"正是这个界面最容易被误读的地方。

    同一个 key 只出现一次：现算的那条带上它的裁决，库里的记录不再单独列一遍。
    """
    by_key = {record.proposal_key: record for record in records}
    rows = [
        _proposal_row(
            proposal,
            by_key.get(proposal.id),
            live_recipe_version=live_recipe_version,
        )
        for proposal in proposals
    ]
    derived = {proposal.id for proposal in proposals}
    rows.extend(
        _proposal_row_from_record(record, live_recipe_version=live_recipe_version)
        for record in records
        if record.proposal_key not in derived
    )
    return rows


def _effective_status(
    record: LearningProposalRecord, *, live_recipe_version: str | None
) -> tuple[str, str | None]:
    """裁决状态在**读的时候**要不要显示成 `expired`。

    只有 `approved` 会过期，理由不是"批准比较容易坏"，而是过期说的是
    "这条批准指的是**哪一版配方**"——一个被否决的方向不会因为配方换版而变得
    不再被否决，那件事没有"过期"可言。

    比不出结果时**不过期**：库里没记版本（目标不在 StyleRecipe 上、或当时取不到
    基准配方）时无从判断，此时把一条批准说成过期就是编造。宁可显示 `approved`。
    """
    if record.status != ProposalStatus.APPROVED.value:
        return record.status, None
    if not record.current_recipe_version or not live_recipe_version:
        return record.status, None
    if record.current_recipe_version == live_recipe_version:
        return record.status, None
    return ProposalStatus.EXPIRED.value, PROPOSAL_EXPIRED_REASON


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
                origin=run.origin,
            )
            for run in runs
        ]

    async def _queue_items(self, runs) -> list[ReviewQueueItem]:
        """待审 Run → 队列条目。草稿正文来自 draft 表，guardrail 来自 checkpoint。"""
        run_ids = [run.id for run in runs]
        async with self._session_factory() as session:
            drafts = await drafts_by_run(session, run_ids)
            reviews = await reviews_by_run(session, run_ids)
            # 同一个会话里批量取，理由同上面两条：列表页不能有 N+1。
            auto_reviews = await auto_reviews_by_run(session, run_ids)
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
                    origin=run.origin,
                    auto_review=_auto_review_summary(auto_reviews.get(run.id)),
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
            # 自动审阅者的判断。**只读地下发，不参与任何判据**——判据读的是
            # checks 的原始取值，这里给出的只是"机器为什么这么判"。
            auto_reviews = await auto_reviews_by_run(session, [run.id])
            # 这个 Run 的已发布帖子自己的观察序列。取不到（没发布 / 没有快照）
            # 就是空列表，由 `observation_summary` 变成 `None`，不是一行 0。
            metrics_snapshots = (
                await list_post_metrics_snapshots(session, published_post_id=published.id)
                if published is not None
                else []
            )

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
            origin=run.origin,
            auto_review=_auto_review_summary(auto_reviews.get(run.id)),
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
            # 与 quality 同一处门控、同一个理由：topic 链路的 Run 没有 recipe，
            # 冒出一个空的"写作结构"块会让人以为 Agent 主动选了"不指定结构"。
            # （`style_summary` 自己也会在无 recipe 时返回 None，这里是双保险：
            # 将来若给 topic 链路也接上 recipe，这个门控是明确该重新考虑的一处。）
            style=style_summary(state) if state.get("goal") else None,
            # 第三处同一门控，同一条理由：V1 topic 链路没有"形成判断"这一步。
            viewpoint=viewpoint_summary(state) if state.get("goal") else None,
            # 第四处同一门控：反馈上下文是 Agent 运行才有的东西（5B.4 之前
            # 的 checkpoint 里连这个键都不存在，`feedback_summary` 会如实
            # 报成"这次运行没读过"，而不是伪造一个空的上下文）。
            feedback=feedback_summary(state) if state.get("goal") else None,
            # 这一项**不**门控于 goal：发布链路是 V1 就有的，topic 链路的 Run
            # 也会发帖、也会被采集到观察。
            performance=observation_summary(metrics_snapshots, published),
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
            # 一页一次，不是一行一次：见 `latest_metrics_by_post` 的说明。
            # 没有快照的帖子不在这个字典里，于是 `latest_metrics` 为 None，
            # 界面显示"暂无数据"而不是 0。
            metrics = await latest_metrics_by_post(session, [row.id for row in rows])

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
                    latest_metrics=(
                        observation_summary([metrics[row.id][0]], row, observation_count=metrics[row.id][1])
                        if row.id in metrics
                        else None
                    ),
                )
                for row in rows
            ],
            page=_page(page, page_size, total),
        )

    # ---------- Feedback ----------

    async def feedback(self) -> FeedbackOverview:
        """自己过去发布内容的观察一览（Phase 5B.4）。

        ## 现算，不读那个"证据文件"

        采集脚本不产出任何 JSON 证据文件，报告由存下来的快照现算——
        这正是"报告是观察的纯函数"的意思。文件是给人看的证据，
        不是 API 的数据源；把它当数据源会让接口跟着一次人工运行的产物走。

        ## 一条快照都没有时返回原因，不返回零

        `available=False` + 原因，同 `MetricAvailability` 的口径。
        返回一串 0 会被读成"采到了，结果没人互动"。
        """
        async with self._session_factory() as session:
            snapshot_count = await count_post_metrics_snapshots(session)
            if snapshot_count == 0:
                return FeedbackOverview(
                    available=False,
                    reason=X_PERFORMANCE_UNAVAILABLE_REASON,
                    limitations=[
                        "还没有任何观察快照：先运行 scripts/refresh_post_metrics.py "
                        "采集已发布帖子的公开计数。"
                    ],
                )
            report = await load_feedback_report(session)
            # 与报告同一个读取上界：帖子摘要从这同一批观察里分组得到，
            # 两个数字来自两批不同的行会让"n 条观察 / 覆盖 m 条帖子"自相矛盾。
            rows = await list_recent_post_metrics_snapshots(
                session, limit=FEEDBACK_SNAPSHOT_READ_LIMIT
            )
            posts = await list_published_posts_with_x_id(session)
        posts_by_id = {str(post.id): post for post in posts}

        return FeedbackOverview(
            available=True,
            reason=X_PERFORMANCE_AVAILABLE_NOTE,
            sample_count=report.sample_count,
            snapshot_count=snapshot_count,
            post_count=len({row.published_post_id for row in rows}),
            latest_observed_at=max((row.observed_at for row in rows), default=None),
            signals=[
                FeedbackSignalRow(
                    kind=signal.kind.value,
                    statement=signal.statement,
                    metric=signal.metric,
                    value=signal.value,
                    comparison_group=signal.comparison_group,
                    sample_count=signal.sample_count,
                    confidence=signal.confidence,
                    evidence=list(signal.evidence),
                    window=signal.window,
                )
                for signal in report.signals
            ],
            limitations=list(report.limitations),
            posts=_feedback_post_summaries(rows, posts_by_id),
        )

    # ---------- Learning readiness ----------

    async def learning(
        self, *, window: str | None = None, hard_max_length: int | None = None
    ) -> LearningOverview:
        """自己过去发布内容的**学习准备度**一览（Phase 5C / 5D）。

        ## 它是控制台里唯一一个"聚合接口读 checkpoint"的地方

        分组维度只存在于 checkpoint（见模块抬头那条边界）。所以这个接口
        与别的聚合接口不同：它**按条**读每条已发布帖子的 checkpoint，代价随
        帖子数增长，因此有读取上界 `LEARNING_POST_READ_LIMIT`，到达上界时
        如实写进 `limitations`。它被刻意做成一个**独立**端点，而不是塞进
        `/api/admin/analytics` 或 `/api/admin/feedback`：那会把一次重读变成
        每次打开首页都要付的代价。

        ## 读不到 checkpoint 不等于没有数据

        `build_post_performance_profile` 读不到当时的决策时退化成 `None`
        （并在 limitations 里说明），所以那些帖子仍然进入"样本量"的统计——
        它们只是不进任何分组。把两者混成一个"没有数据"会让
        "checkpoint 被回收了"看起来像"这个账号还没发过东西"。

        ## 数据不足时返回原因，不返回零

        `available=False` 只在**一条记录都没有**时出现。样本不足是
        `available=True` + `readiness.learning_ready=False` + 明确的原因——
        那是一个**结论**，不是一次失败。

        ## Phase 5D 起多带一个 `proposal_count`

        这个字段是**现算的条数**，不是一次查询：它来自与 `GET
        /api/admin/learning/proposals` 完全相同的那一步生成。只报一个数字而不是
        把提案整个塞进这里，是为了让 5C 的响应形状保持不变，也让"提案有多重"
        这件事留在它自己的端点里（那个端点还要读一次 checkpoint）。

        闸门关着时它**必然是 0**（工作单第七节），而且那不是"没算出来"——
        是"算过了，结论就是零条"。
        """
        async with self._session_factory() as session:
            report = await load_learning_report(session, self._graph, window=window)
        base_recipe = await self._base_recipe(hard_max_length=hard_max_length)

        proposals = generate_proposals(report, base_recipe=base_recipe)
        readiness = report.readiness
        available = bool(report.records)
        return LearningOverview(
            available=available,
            reason=LEARNING_AVAILABLE_NOTE if available else LEARNING_UNAVAILABLE_REASON,
            observation_window=report.observation_window,
            readiness=LearningReadinessRow(
                learning_ready=readiness.learning_ready,
                reason=readiness.reason,
                observation_window=readiness.observation_window,
                sample_count=readiness.sample_count,
                post_count=readiness.post_count,
                checks=[check.model_dump() for check in readiness.checks],
                automatic_modification=readiness.automatic_modification,
                limitations=list(readiness.limitations),
            ),
            sample_count=readiness.sample_count,
            post_count=report.post_count,
            cohort_count=len(report.cohorts),
            valid_comparison_count=len(report.valid_comparisons),
            proposal_count=len(proposals),
            cohorts=[
                LearningCohortRow(
                    dimension=cohort.dimension.value,
                    value=cohort.value,
                    observation_window=cohort.observation_window,
                    selection_rule=cohort.selection_rule,
                    sample_count=cohort.sample_count,
                    topics=list(cohort.topics),
                    limitations=list(cohort.limitations),
                )
                for cohort in report.cohorts
            ],
            comparisons=[
                LearningComparisonRow(
                    dimension=comparison.dimension.value,
                    metric=comparison.metric.value,
                    observation_window=comparison.observation_window,
                    group_a=comparison.group_a.value,
                    group_b=comparison.group_b.value,
                    sample_count_a=comparison.group_a.sample_count,
                    sample_count_b=comparison.group_b.sample_count,
                    median_a=comparison.group_a.median,
                    median_b=comparison.group_b.median,
                    observed_difference=comparison.observed_difference,
                    valid=comparison.valid,
                    invalid_reason=comparison.invalid_reason,
                    statement=comparison.statement,
                    limitations=list(comparison.limitations),
                )
                for comparison in report.comparisons
            ],
            evidence=[
                LearningEvidenceRow(
                    id=item.id,
                    type=item.type.value,
                    statement=item.statement,
                    sample_count=item.sample_count,
                    comparison_group=item.comparison_group,
                    observation_window=item.observation_window,
                    metric=item.metric,
                    dimension=item.dimension,
                    source_snapshot_ids=list(item.source_snapshot_ids),
                    source_post_ids=list(item.source_post_ids),
                    source_x_post_ids=list(item.source_x_post_ids),
                    confidence=item.confidence,
                    limitations=list(item.limitations),
                )
                for item in report.evidence
            ],
            limitations=list(report.limitations),
        )

    # ---------- Learning proposals ----------

    async def _base_recipe(self, *, hard_max_length: int | None = None):
        """当前生产会用到的 StyleRecipe。

        **走 `resolve_recipe` 而不是自己拼一个**：提案里的
        `current_recipe_version` 要能与"运行期实际拿到的那一版"对上，如果这里
        另写一条构造路径，两边的指纹迟早会分叉，而分叉的表现是版本号看起来
        对不上——那正是版本号要回答的问题。

        `hard_max_length`（平台正文上限）由 API 层从 persona 递进来，与
        `review_queue` 的做法一致：`app/style/` 是离线语料层，它不该 import
        `app/agent/`。不传表示"这一层不知道平台约束"，此时版本指纹里那一格
        是空——与运行期不传时完全一致。
        """
        return await resolve_recipe(self._session_factory, hard_max_length=hard_max_length)

    async def learning_proposals(
        self, *, window: str | None = None, hard_max_length: int | None = None
    ) -> ProposalsOverview:
        """提案一览（`GET /api/admin/learning/proposals`，Phase 5D）。

        ## 现算 + 叠加已存裁决

        提案本身是纯函数派生的，所以这里**先算一遍**；然后把它与库里已存的裁决
        按确定性 key 叠起来。三件事因此同时成立：

        - 没人裁决过的提案照常出现，状态 `proposed`（不落库）；
        - 裁决过的提案显示**人做过的决定**，而不是"还没决定"；
        - 当时裁决过、现在算不出来的提案**仍然列出来**（`stale_reason` 说明原因）。

        第三条是这个设计唯一会让人意外的地方，但反过来做更糟：一条批准过、
        后来数据变了的历史裁决如果从界面上消失，读的人会以为"从来没人批准过
        什么"。

        ## 闸门关着时是零条，而不是少几条

        `learning_ready=false` ⇒ 生成阶段就返回空（工作单第七节）。这里不再做
        第二次过滤——下游过滤是"可能会忘"的那种保护。
        """
        report, proposals, records = await self._proposal_snapshot(
            window=window, hard_max_length=hard_max_length
        )
        live_version = recipe_version(
            await self._base_recipe(hard_max_length=hard_max_length)
        )
        rows = _merge_proposals(proposals, records, live_recipe_version=live_version)
        available = bool(report.records) or bool(records)

        if not available:
            reason = LEARNING_UNAVAILABLE_REASON
        elif not report.readiness.learning_ready:
            reason = PROPOSALS_GATE_CLOSED_REASON
        else:
            reason = PROPOSALS_AVAILABLE_NOTE

        return ProposalsOverview(
            available=available,
            reason=reason,
            observation_window=report.observation_window,
            learning_ready=report.readiness.learning_ready,
            proposal_count=len(proposals),
            reviewable_count=sum(1 for row in rows if row.reviewable),
            decided_count=len(records),
            proposals=rows,
            limitations=list(report.limitations),
        )

    async def review_learning_proposal(
        self,
        proposal_id: str,
        *,
        action: str,
        note: str | None = None,
        window: str | None = None,
        hard_max_length: int | None = None,
    ) -> LearningProposalRow:
        """记下人对一条提案的裁决（`POST .../proposals/{id}/review`）。

        ## 只能裁决**现在成立**的提案

        先按 id 现算一遍，算不出来就拒绝（`not_found`）。刻意**不**回退到库里
        存过的内容：能回退的话，一条已经不再成立的提案照样能被"批准"，而那条
        批准在事后读起来与一条正常批准毫无区别。这是本章唯一的写路径，
        它宁可拒答，也不给一个会夸大自己依据的答案。

        ## 写入之后什么都不发生

        这正是工作单第十一、三十五节要的：批准**只**记一条裁决，供下一阶段
        参考。本模块不 import 任何能改 Prompt / StyleRecipe / 权重 / 阈值 /
        预算的东西——`app/services/admin.py` 到 `app/repositories.py` 的箭头
        只通向数据库。
        """
        _, proposals, _ = await self._proposal_snapshot(
            window=window, hard_max_length=hard_max_length
        )
        proposal = next((item for item in proposals if item.id == proposal_id), None)
        if proposal is None:
            raise LearningProposalReviewError(
                PROPOSAL_REVIEW_NOT_FOUND,
                f"proposal {proposal_id!r} is not produced by the current observation "
                "and cannot be reviewed",
            )

        status = PROPOSAL_ACTION_STATUS[action]
        async with self._session_factory() as session:
            record = await insert_learning_proposal_decision(
                session,
                proposal_key=proposal.id,
                target=proposal.target,
                current_value=proposal.current_value,
                proposed_value=proposal.proposed_value,
                rationale=proposal.rationale,
                evidence_ids=proposal.evidence_ids,
                limitations=proposal.limitations,
                sample_count=proposal.sample_count,
                observation_window=proposal.observation_window,
                current_recipe_version=proposal.current_recipe_version,
                proposed_recipe_version=proposal.proposed_recipe_version,
                status=status,
                reviewed_at=_now(),
                note=note,
            )
        if record is None:
            raise LearningProposalReviewError(
                PROPOSAL_REVIEW_ALREADY_DECIDED,
                f"proposal {proposal_id!r} was already reviewed",
            )

        live_version = recipe_version(
            await self._base_recipe(hard_max_length=hard_max_length)
        )
        return _proposal_row(proposal, record, live_recipe_version=live_version)

    async def _proposal_snapshot(
        self, *, window: str | None, hard_max_length: int | None
    ) -> tuple[LearningReport, tuple[LearningProposal, ...], list[LearningProposalRecord]]:
        """报告 + 现算提案 + 已存裁决。三个接口共用的那一步。"""
        async with self._session_factory() as session:
            report = await load_learning_report(session, self._graph, window=window)
            records = await list_learning_proposal_decisions(session)
        base_recipe = await self._base_recipe(hard_max_length=hard_max_length)
        proposals = generate_proposals(report, base_recipe=base_recipe)
        return report, proposals, records

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
            # Phase 5B.4：采集脚本落地之后，"未实现"这句话只在真的没采过时成立。
            snapshot_count = await count_post_metrics_snapshots(session)

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
            # 有快照 → 可用，并带上口径说明（只有公开计数、手工采集）。
            # 没有 → 仍然标成不可用，但原因现在是准确的："还没有采集过"，
            # 而不是从前那句已经变成假话的"尚未实现"。
            x_content_performance=MetricAvailability(
                available=snapshot_count > 0,
                reason=(
                    X_PERFORMANCE_AVAILABLE_NOTE
                    if snapshot_count > 0
                    else X_PERFORMANCE_UNAVAILABLE_REASON
                ),
            ),
        )
