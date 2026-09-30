import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class StyleProfile(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """从一批真实样本算出的**统计特征**（Phase 5B.2 的工作单第十七节）。

    ## 为什么它配得上有一张表

    - checkpoint 装不下它：checkpoint 绑定一次 Run，而 Profile 是 corpus 的
      属性，要在**许多次运行之间**复用。放进 checkpoint 等于每个 Run 重算一遍，
      而且要重算就得拿到全部样本原文——那正是工作单禁止的。
    - 它是**长期语料资产**：今天的 Run 与三个月后的 Run 应当能引用同一版
      Profile，并且能回答"当时用的是哪一版"。

    ## Account Profile 与 Corpus Profile 分开

    `scope` 区分两者，`tracked_account_id` 只在 `scope='account'` 时有值。
    刻意**不**合成一个"超级人格"：工作单第十八节明确要求分开——
    合起来会让"这个特征是谁的"失去答案，而学写法的前提是知道在学谁。

    ## 每个数都要能回溯

    `sample_count` / `sample_window` / `feature_version` / `taxonomy_version`
    跟着每个 Profile 一起存。没有这四个字段，一个 Profile 就是一组无法复核的
    数字：不知道基于几条样本、哪段时间、哪版算法、哪版分类。
    改算法就要进 `feature_version`，否则新旧 Profile 会被当成可直接比较的。
    """

    __tablename__ = "style_profile"

    #: 'account' | 'corpus'。见 `app/style/profile.py::ProfileScope`。
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 仅 `scope='account'` 时非空。corpus 级 Profile 不属于任何单个账号。
    tracked_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tracked_account.id", ondelete="CASCADE"), nullable=True
    )

    # --- 可回溯性 ---
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sample_window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sample_window_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: 算法版本。改了特征定义就要进位——否则两个 Profile 会被误当同口径。
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 结构分类法版本。同上。
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- 标量特征（单位见 app/style/profile.py 的说明）---
    median_length: Mapped[float | None] = mapped_column(Float, nullable=True)
    p25_length: Mapped[float | None] = mapped_column(Float, nullable=True)
    p75_length: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_paragraphs: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 每 100 字。以下同。
    emoji_density: Mapped[float | None] = mapped_column(Float, nullable=True)
    question_density: Mapped[float | None] = mapped_column(Float, nullable=True)
    number_density: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 拉丁字母占非空白字符的比例（0-1）。
    english_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 平均每条的句数。
    mean_sentence_count: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- 分布（JSONB：结构会随分类法演进，用列会把演进变成迁移）---
    #: {structure_type: 占比}
    structure_distribution: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    #: {hook_type: 占比}
    hook_distribution: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: {opening_pattern: 占比}（表层形态，见 app/style/features.py）
    opening_distribution: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    #: {closing_pattern: 占比}
    closing_distribution: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    #: {"p25": .., "p50": .., "p75": ..} 句长分位。
    sentence_length_distribution: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<StyleProfile scope={self.scope} n={self.sample_count}>"


__all__ = ["StyleProfile"]
