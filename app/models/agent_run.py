import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.draft import Draft
    from app.models.research_item import ResearchItem


class AgentRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "agent_run"

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("account.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # Phase 5A 起 topic 允许为空：Agent 链路下主题由 Agent 自己发现，创建 Run 时
    # 还不知道它是什么。等 Planner 选定之后由 finalize 节点写回。
    # 旧行全部有值，因此这不改变任何既有语义。
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Agent 的输入是**目标**而不是题目："今天帮我找一个值得在中文科技 X 圈讨论的 AI 话题"。
    #
    # 这一列同时充当"这次 Run 走哪条拓扑"的判别字段：`goal IS NULL` → 既有的
    # topic 线性链路；`goal IS NOT NULL` → Phase 5A 的 Agent 链路。
    # 刻意不再加一个 graph_kind 列：多一列就多一个可能与 goal 自相矛盾的状态源。
    #
    # 与 topic 的关系是"目标 → 结果"：有 goal 的 Run 最终也会被填上 topic
    # （Agent 选定的那个），所以界面上绝不能拿 goal 顶替 topic 显示。
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    research_items: Mapped[list["ResearchItem"]] = relationship(back_populates="agent_run")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="agent_run")
