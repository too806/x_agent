import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.draft import Draft


class Review(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "review"

    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("draft.id", ondelete="CASCADE"), nullable=False
    )
    # 人工审核决定：pending（待审）→ approved / edited / rejected
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # 审核时刻的正文快照；原 draft 行不被覆盖，编辑稿只存在这里与 Graph state
    original_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    draft: Mapped["Draft"] = relationship(back_populates="reviews")
