from datetime import datetime

from pydantic import BaseModel


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


class RunResponse(BaseModel):
    run_id: str
    status: str
    research_results: list[ResearchResult] = []
    draft_content: str | None = None
    draft_id: str | None = None
    error: str | None = None
