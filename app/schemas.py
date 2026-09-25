from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator


class ResearchResult(BaseModel):
    """X API 搜索结果映射后的研究条目。"""

    platform_post_id: str
    author_username: str | None = None
    content: str
    url: str | None = None
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    published_at: datetime | None = None


class GeneratedDraft(BaseModel):
    """LLM 结构化输出，经 schema validation。"""

    content: str
    hook_type: str
    topic: str


class RunRequest(BaseModel):
    topic: str


class ReviewPayload(BaseModel):
    """interrupt() 交给人工审核者的内容。

    同时是 GET/POST 响应里 review 字段的形状，字段与 human_review 节点
    构造的 payload 一一对应。
    """

    type: str = "content_review"
    run_id: str
    draft_id: str | None = None
    content: str | None = None
    guardrail_passed: bool = False
    guardrail_reasons: list[str] = []


class ReviewRecord(BaseModel):
    """review 表里已保存的审核记录（人工决定之后才存在）。"""

    review_id: str
    action: str
    original_content: str
    edited_content: str | None = None
    reject_reason: str | None = None
    reviewed_at: datetime | None = None


class RunResponse(BaseModel):
    run_id: str
    status: str
    research_results: list[ResearchResult] = []
    draft_content: str | None = None
    draft_id: str | None = None
    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = []
    human_action: str | None = None
    final_content: str | None = None
    review: ReviewPayload | None = None
    error: str | None = None


class PublishRecord(BaseModel):
    """published_post 行的对外形状。

    同时返回 body 与 draft_id：edit 路径下 body 是人工改写稿、与原 draft.content
    不同，所以这一行**不能自证"我是从哪来的"**。V1 接受这一点，把两者都摆出来。
    """

    status: str
    draft_id: str | None = None
    body: str | None = None
    x_post_id: str | None = None
    published_at: datetime | None = None
    last_error: str | None = None
    # 最近一次状态写入时刻。人工裁决"结果未知"时需要它来判断卡了多久——
    # created_at 在重试过的行上不会刷新，单独看不出来。
    updated_at: datetime | None = None


def publish_record_from_row(row) -> PublishRecord:
    """published_post 行 → 对外形状。

    单一实现：`app/main.py`（发布接口）与 `app/services/admin.py`（控制台）都用它。
    两侧各自复制一份的话，将来给 published_post 加一列必须记得改两处——漏掉的那
    一处会静默少一个字段，而且不会有任何测试报错（两边都只是"少了个键"）。
    """
    return PublishRecord(
        status=row.status,
        draft_id=str(row.draft_id),
        body=row.body,
        x_post_id=row.x_post_id,
        published_at=row.published_at,
        last_error=row.last_error,
        updated_at=row.updated_at,
    )


class PublishResponse(BaseModel):
    """发布结果。

    outcome 是给调用方做分支判断的；status 是 published_post 的落库状态。
    两者不总是一致，这是刻意的：outcome="unknown" 时 status 是 "publish_unknown"，
    表示"结果未知、需要人工裁决"。
    """

    run_id: str
    outcome: str
    publish: PublishRecord | None = None
    error: str | None = None


class RunDetailResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    draft_id: str | None = None
    draft_content: str | None = None
    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = []
    human_action: str | None = None
    final_content: str | None = None
    review: ReviewRecord | None = None
    publish: PublishRecord | None = None
    # Run **真正终结**的时刻。waiting_for_review / approved / edited / publishing /
    # publish_unknown 一律为空——"流水线跑完了"不等于"这个 Run 结束了"，发布是
    # 另一条时间线，由 published_post 表达。
    finished_at: datetime | None = None
    error: str | None = None


def _strip_not_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class ApproveDecision(BaseModel):
    action: Literal["approve"]


class EditDecision(BaseModel):
    action: Literal["edit"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def _normalize_content(cls, value: str) -> str:
        return _strip_not_blank(value)


class RejectDecision(BaseModel):
    action: Literal["reject"]
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        return _strip_not_blank(value)


# 人工审核决定：以 action 为判别字段的联合类型，HTTP 层与 Graph 内都走同一套校验。
ReviewDecision = Annotated[
    ApproveDecision | EditDecision | RejectDecision, Field(discriminator="action")
]
ReviewDecisionAdapter = TypeAdapter(ReviewDecision)

REVIEW_ACTION_STATUS = {"approve": "approved", "edit": "edited", "reject": "rejected"}


class MarkPublishedDecision(BaseModel):
    """人工上 X 看过，确认**确实发出去了** → 把状态补成 published。"""

    action: Literal["mark_published"]
    # 必须由人从 X 页面抄回来。不自动推导：这个值只有亲眼看过才有意义，
    # 系统自己猜出来的 post_id 正是"结果未知"要解决的问题，不是它的答案。
    x_post_id: str = Field(min_length=1)

    @field_validator("x_post_id")
    @classmethod
    def _normalize_post_id(cls, value: str) -> str:
        return _strip_not_blank(value)


class RetryPublishDecision(BaseModel):
    """人工上 X 看过，确认**确实没有** → 改回 failed，之后可显式重发。"""

    action: Literal["retry"]
    # 强制显式确认：误触的代价虽然只是"状态改错"，但这个字段让调用方
    # 不可能"顺手"发出这个决定。Literal[True] 意味着必须传 true，不能省略。
    confirm_maybe_duplicate: Literal[True]


# 发布裁决：与 ReviewDecision 同一套判别联合模式。
PublishResolution = Annotated[
    MarkPublishedDecision | RetryPublishDecision, Field(discriminator="action")
]
PublishResolutionAdapter = TypeAdapter(PublishResolution)


# ---------------------------------------------------------------------------
# 运营控制台（Phase 4b）只读响应模型
#
# 这些模型**只描述读取**。控制台不新增任何状态、不改变任何既有语义：所有写操作
# 仍然调用 Phase 3/4a 的 review 与 publish 端点，前端不碰数据库。
#
# 字段命名遵循两条约定：
#   - run_status 一定来自 agent_run.status（运行状态镜像）
#   - publish_status 一定来自 published_post.status（发布状态权威）
# 两者**绝不合并成一个字段**：它们可能合法地互相矛盾（比如发布失败时 Run 仍是
# approved），合并显示会把这个事实藏起来。
# ---------------------------------------------------------------------------


class Page(BaseModel):
    """服务器端分页元信息。前端据此渲染翻页控件，不需要自己数。"""

    page: int
    page_size: int
    total: int
    pages: int


class ResearchItemRecord(BaseModel):
    """research_item 表里真实落库的一条研究材料。"""

    item_id: str
    platform_post_id: str
    author_username: str | None = None
    content: str
    source_url: str | None = None
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    published_at: datetime | None = None
    created_at: datetime


class DraftRecord(BaseModel):
    draft_id: str
    content: str
    status: str
    version: int
    model_name: str | None = None
    prompt_version: str | None = None
    generated_at: datetime | None = None


class RunSummary(BaseModel):
    """Run 列表行。发布状态可能为 None —— 表示**没有发布记录**。

    刻意不用 None 之外的任何值去"兜底"（比如拿 run_status 顶替）：没有
    published_post 行就是没有，UI 显示 `—`。
    """

    run_id: str
    topic: str
    run_status: str
    review_status: str | None = None
    publish_status: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class RunListPage(BaseModel):
    items: list[RunSummary]
    page: Page


class ReviewQueueItem(BaseModel):
    """待人工审核的 Run。

    guardrail_* 来自 LangGraph checkpoint（数据库里没有这一列），因此可能为
    None —— 表示"读不到"，不是"没通过"。UI 必须把 None 显示成 `—`。
    """

    run_id: str
    topic: str
    run_status: str
    created_at: datetime
    draft_id: str | None = None
    draft_preview: str | None = None
    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = []


class ResolutionQueueItem(BaseModel):
    """需要人工裁决的发布尝试（publishing / publish_unknown）。"""

    run_id: str
    topic: str | None = None
    run_status: str | None = None
    publish: PublishRecord
    detail: str | None = None


class TrendPoint(BaseModel):
    date: str  # ISO 日期（UTC 自然日）
    count: int


class OverviewKpis(BaseModel):
    total_runs: int
    waiting_for_review: int
    # 以下三项一律来自 published_post —— 发布状态的权威。
    published: int
    publish_failed: int
    publish_unknown: int
    # publishing 单列：与 publish_unknown 同为"不可自动重试"，但含义不同
    # （可能仍在飞行中）。藏掉它会让一个卡住的发布在首页完全不可见。
    publishing: int


class OverviewResponse(BaseModel):
    window_days: int
    kpis: OverviewKpis
    runs_trend: list[TrendPoint]
    published_trend: list[TrendPoint]
    review_queue: list[ReviewQueueItem]
    review_queue_total: int
    resolution_queue: list[ResolutionQueueItem]
    resolution_queue_total: int


class AdminRunDetail(BaseModel):
    """Run 的完整生命周期视图。

    guardrail_passed / guardrail_reasons / human_action / final_content 来自
    checkpoint，其余来自数据库。两处来源在这里**只读地**汇合，不写回任何一边。
    """

    run_id: str
    topic: str
    run_status: str
    review_status: str | None = None
    publish_status: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = []
    human_action: str | None = None
    final_content: str | None = None

    research_items: list[ResearchItemRecord] = []
    draft: DraftRecord | None = None
    review: ReviewRecord | None = None
    publish: PublishRecord | None = None


class PublishedRow(BaseModel):
    """published_post 行 + 它所属 Run 的少量展示字段。

    `body` 是**实际发布出去的正文**：edit 路径下它是人工改写稿，与原
    draft.content 不同。所以这里同时给出 draft_id，让这一行能够自证来源。
    """

    publish_id: str
    run_id: str
    draft_id: str
    topic: str | None = None
    body: str
    status: str
    x_post_id: str | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


class PublishedListPage(BaseModel):
    items: list[PublishedRow]
    page: Page


class ReviewQueuePage(BaseModel):
    items: list[ReviewQueueItem]
    page: Page
    # 编辑稿的字数上限，取自已加载的 persona 配置。界面用它渲染 "N / 140"。
    #
    # 必须由后端下发而不是前端写死：这个值同时是发布前的硬校验
    # （`app/services/publish.py` 里 `len(content) > persona.max_length` → invalid），
    # 界面抄一份就会在配置改动后开始说谎，而它存在的意义恰恰是别让人写出会被打回的稿子。
    #
    # 口径：这是 **Python `len()`**，字符数，不是 X 的加权字符数计算。中文一个字算 1。
    content_max_length: int


class MetricAvailability(BaseModel):
    """指标是否可计算。不可计算时必须给出原因，**不得**用 0 冒充。"""

    available: bool
    reason: str | None = None


class ReviewDecisionDistribution(BaseModel):
    approve: int = 0
    edit: int = 0
    reject: int = 0
    other: int = 0


class PublishOutcomeDistribution(BaseModel):
    published: int = 0
    failed: int = 0
    publish_unknown: int = 0
    publishing: int = 0
    # 口径说明随数据一起下发：published_post 每个 Run 最多一行、失败重试复用
    # 同一行，所以这里是"每个 Run 的当前结局"，不是"尝试次数"。
    note: str


class AverageRunDuration(BaseModel):
    available: bool
    average_seconds: float | None = None
    sampled_runs: int = 0
    total_runs: int = 0
    reason: str | None = None


class TopicCount(BaseModel):
    topic: str
    count: int


class AnalyticsResponse(BaseModel):
    window_days: int
    # 窗口内的 Run 总数。日粒度序列在 daily_runs —— 两者刻意不重复下发同一份数据。
    run_volume: int
    review_decisions: ReviewDecisionDistribution
    publish_outcomes: PublishOutcomeDistribution
    daily_runs: list[TrendPoint]
    daily_published: list[TrendPoint]
    average_run_duration: AverageRunDuration
    topic_distribution: list[TopicCount]
    x_content_performance: MetricAvailability


class AdminErrorResponse(BaseModel):
    """控制台错误信息的对外形状。只带安全文案，绝不带 traceback。"""

    detail: str
