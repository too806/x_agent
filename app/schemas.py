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
