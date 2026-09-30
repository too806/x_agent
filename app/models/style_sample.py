import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.tracked_account import TrackedAccount


class StyleSample(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """一条**经过筛选、进入 Style Corpus**的真实帖子（Phase 5B.2）。

    ## 为什么不能复用 `research_item`

    三个理由，任何一条单独成立就足够：

    1. `research_item.agent_run_id` 是 `NOT NULL` + `ON DELETE CASCADE`。
       删掉一次 Run 会连带删掉它采到的全部样本——而 corpus 的生命周期
       **必须长于**任何一次 Run，否则 Profile 会随运行历史的清理而变。
    2. `research_item` 刻意**没有** `UNIQUE(platform_post_id)`：多轮检索取回
       同一条帖子，在趋势打分里就该算多条材料。corpus 恰好相反，它需要一个
       去重键，否则同一条爆款会被数很多遍，把 Profile 拽向它。
    3. 语义不同。"这次运行取回的材料"与"被接纳进语料的样本"是两个判断，
      同一张帖子可以是前者而不是后者（互动不够、是转发、正文不完整）。

    ## 时间字段：沿用，不发明第三套

    - `published_at`：原帖在 X 上的发布时间（来源给的）。
    - `observed_at`：我们取回它的时间。
    - `created_at`：本行写库的时间（mixin 给的）。

    与 `research_item` 完全同一套语义。多一套时间口径只会让"这条样本多新"
    变成一个每次都要重新确认的问题。

    ## 什么**不**存在这里

    **确定性特征一律不落库。** 长度、句长分布、emoji 率、英文比例……
    全部能从 `content` 重算，而且重算结果逐位相同（见 `app/style/features.py`
    的纯函数契约）。存一份等于制造一个将来会漂移的第二来源，而"Profile 里
    这个数到底哪来的"会因此变成一个没有答案的问题。

    存下来的是**不能重算**的东西：
    - 来源事实（原文、互动计数、两个时间）；
    - 采集当时的判定（`is_repost` / `is_link_only` / `is_reply`）——它们来自
      X 的 `referenced_tweets` 与正文形态，事后从 `content` 单看是判不出来的；
    - 筛除结论与理由；
    - LLM 的语义标注与它的模型/版本（标注过一次就该留痕，不该重跑）。
    """

    __tablename__ = "style_sample"

    tracked_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tracked_account.id", ondelete="CASCADE"), nullable=False
    )

    # --- 来源侧事实 ---
    platform_post_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    author_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 四个 observed engagement 计数，原样记录 X 给的值，不做解释、不加权。
    #: **它们不是质量真值**（工作单第十二节）：只是筛选的辅助门槛与样本属性，
    #: 不参与 Profile 里的任何质量打分。
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reposts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quote_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- 采集当时的判定（无法从 content 重算，必须存）---
    #: 转发/引用他人内容。学的是"他怎么写自己的话"，不是"他转了什么"。
    is_repost: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 正文只有链接（去掉链接后没有实质内容）。学不到写法。
    is_link_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 回复别人的帖子。与 `is_repost` 分开存，理由同它们为什么都要存：
    #: X 的 `referenced_tweets` 是一个混装列表（retweeted / quoted /
    #: replied_to），只看"非空"会把回复标成转发。存下来之后，"这批语料里
    #: 有多少是回复"是一个能回答的问题，而不是一个需要重新取数的猜测。
    is_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- 筛选结论 ---
    #: 是否被接纳进 corpus。被排除的样本**仍然留行**并记下理由——
    #: 否则"为什么这批样本这么少"永远无法回答，而采集规模正是要如实记录的。
    included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- LLM 语义标注（offline 预计算，见 app/style/annotate.py）---
    content_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    structure_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hook_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    has_claim: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_evidence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_counterpoint: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_question: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_list: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_number: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_conclusion: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: 标注来源留痕。没有这三个字段，就没法回答"这个标注是哪版模型什么时候给的"，
    #: 而重跑标注会静默改变 Profile。
    annotation_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    annotation_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    annotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: 采集置信度留痕：LLM 对这次结构标注的自评（0-1，可空）。
    #: 可空是刻意的——没标注过就是 NULL，不是 0（0 会被读成"极不可信"）。
    annotation_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    account: Mapped["TrackedAccount"] = relationship(back_populates="samples")

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<StyleSample {self.platform_post_id} included={self.included}>"


__all__ = ["StyleSample"]
