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
