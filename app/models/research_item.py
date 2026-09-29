import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun


class ResearchItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """从 X 真实取回的一条讨论材料。

    与 `CandidateTopic` / `EvidencePack` / `Angle` / `Candidate` 的边界是刻意的：
    **本表是外部来源的持久化数据，不是 Agent 的工作状态**。所以它值得有列，
    而且值得如实记录来源口径；而 Agent 的中间推理产物一律只活在 checkpoint 里
    （见 alembic a1f0c7d3b5e2 的说明）。Phase 5B.1 只在本表上做最小扩展。

    三个时间字段，语义互不替代：

    - `published_at`：原帖在 X 上的发布时间（来源给的）。
    - `observed_at`：Agent 从 X API 取回它的时间（Phase 5B.1 新增）。
    - `created_at`：本行写库的时间（mixin 给的）。

    把 `observed_at` 记成显式的一列，是因为 trend 的时效判断问的是"这条讨论
    相对**我看到它的那一刻**有多新"，而不是"这行记录什么时候写的"。两者在
    正常路径上几乎相同，但一旦补抓、重跑或迁移数据就会分开——分开之后
    `created_at` 会让一条三天前的讨论看起来刚刚发生。
    """

    __tablename__ = "research_item"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
    )
    platform_post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    author_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 四个 observed engagement 计数，原样记录 X 给的值，不做解释、不加权。
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reposts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quote_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 可空：Phase 5B.1 之前的行没有这个信息，历史数据一律为 NULL。
    # 用 NULL 而不是拿 created_at 回填——回填等于编一个从没观察过的时刻。
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_run: Mapped["AgentRun"] = relationship(back_populates="research_items")
