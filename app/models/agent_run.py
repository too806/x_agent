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

#: 谁发起了这个 Run。`manual` = 人在控制台点的（Phase 1 以来的全部行为）；
#: `scheduled` = 自动发布调度器建的。
#:
#: 这一列不是给界面看的分类标签，它是**三个功能的承重墙**：
#:   1. 当日额度只能按它界定范围——库里同时存在人发起的 Run，不区分就没法计数；
#:   2. 影子模式的一致率需要分母，而分母只能是自动发起的那批；
#:   3. 回收残留 claim 时要分清"该收的自动 Run"和"不许碰的人 Run"。
#:
#: 刻意**不**用"goal 等于配置里的 goal"去推断：人完全可以提交同一个 goal，
#: 那种推断会把人的 Run 误判成自动的。来源是事实，必须记下来，不能猜。
ORIGIN_MANUAL = "manual"
ORIGIN_SCHEDULED = "scheduled"


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
    # 见 ORIGIN_MANUAL / ORIGIN_SCHEDULED。默认 manual 让既有行无需回填即自洽——
    # 这一列出现之前，所有 Run 确实都是人发起的。
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ORIGIN_MANUAL, server_default=ORIGIN_MANUAL
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    research_items: Mapped[list["ResearchItem"]] = relationship(back_populates="agent_run")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="agent_run")
