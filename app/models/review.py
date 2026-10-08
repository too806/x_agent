import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.draft import Draft

#: 这个决定是谁做的。人走控制台是 `human`；自动发布里的自动审阅者写 `auto`。
#:
#: 没有它，审核记录就答不出"这条帖子是机器人放行的还是人放行的"——
#: 而发布本身不带这个信息（两条路最终都走同一个 PublishService）。
#: 影子模式要靠它和 auto_review 表配对算一致率，正式模式要靠它做审计。
DECIDED_BY_HUMAN = "human"
DECIDED_BY_AUTO = "auto"


class Review(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "review"

    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("draft.id", ondelete="CASCADE"), nullable=False
    )
    # 人工审核决定：pending（待审）→ approve / edit / reject
    # 注意这里存的是**动作**（approve / edit / reject），不是 Run 的状态。
    # 映射关系见 app/schemas.py 的 REVIEW_ACTION_STATUS：
    # approve → approved、edit → edited、reject → rejected。
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # 审核时刻的正文快照；原 draft 行不被覆盖，编辑稿只存在这里与 Graph state
    original_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 默认 human：这一列出现之前写下的每一行，确实都是人做的决定。
    decided_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DECIDED_BY_HUMAN, server_default=DECIDED_BY_HUMAN
    )

    draft: Mapped["Draft"] = relationship(back_populates="reviews")
