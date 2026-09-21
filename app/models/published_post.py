import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.draft import Draft

# 发布状态。四个值由本表单独承载，不借用 agent_run.status 的流水线语义。
#
# `publishing` 与 `publish_unknown` 都表示"绝不可自动重试"，区别在**我们手上有什么信息**：
#   publishing      尝试已提交，但本地对结果一无所知（进程崩溃 / 被 kill，或仍在飞行中）
#   publish_unknown 尝试已经结束，本地拿到了信息却无法判定（5xx / 2xx 无 data.id /
#                   读超时 / 连接中断但可能已送达）
# 两者都需要人工上 X 看过之后裁决，但运维含义不同：前者可能还在发，后者一定已经结束了。
STATUS_PUBLISHING = "publishing"
STATUS_UNKNOWN = "publish_unknown"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"

# 停留在这些状态时，任何自动路径都不允许再次调用 X。
UNRESOLVED_STATUSES = (STATUS_PUBLISHING, STATUS_UNKNOWN)

# 唯一约束名。repositories 需要按名字判定"是不是并发抢占撞的"，才能把
# 唯一冲突翻译成冲突而不是冒泡成 500，因此名字是接口的一部分。
UQ_AGENT_RUN_ID = "uq_published_post_agent_run_id"


class PublishedPost(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """一次发布尝试的记录，同时是"是否已经发过"的唯一凭据。

    行的存在即代表"尝试已提交"；`created_at` 是尝试发起时刻（server default），
    因此即使进程在调用 X 期间崩溃，这条记录也一定在库里。

    - `publishing`：尝试已提交，结果未知（正在发 / 崩溃）。这是个"停留态"：
      进程崩溃后不会被自动改写，恢复时也无法与"正在发布"区分——这是刻意的，
      见 app/services/publish.py 的失败方向说明。
    - `publish_unknown`：尝试已结束但结果无法判定（5xx / 超时 / 2xx 无 id）。
      同样只能人工裁决，但可以确定"不会再有后续写入了"。
    - `published`：已确认发布成功，x_post_id 必非空。
    - `failed`：确定没有发出去（X 明确拒绝），可以重试。

    已知限制（V1 接受）：
    - draft_id / agent_run_id 均为 ON DELETE CASCADE：删除 Run 会连带删掉
      "这条推文发过了"的证据。当前没有任何删除接口，风险为 0；将来加清理任务
      时必须先处理这里。
    """

    __tablename__ = "published_post"
    # 一个 Run 最多一条发布记录。
    # 注意：采纳 agent_run 层的发布 CAS 之后，**并发的主要串行化点是 agent_run 的
    # 条件 UPDATE**（两个请求都改同一行，行锁天然排队）。这条唯一约束因此退回为
    # 第二道防线——它守护的是"一个 Run 最多一条发布记录"这个 schema 级不变量，
    # 防的是应用层 bug 绕过 CAS 直接插入，而不是并发本身。
    __table_args__ = (UniqueConstraint("agent_run_id", name=UQ_AGENT_RUN_ID),)

    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("draft.id", ondelete="CASCADE"), nullable=False
    )
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
    )
    # 实际发布的正文。edit 路径下这里是人工改写稿，与原 draft.content 不同。
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 刻意不给 Python 侧默认值：四个状态的语义差别是本表唯一的安全属性，
    # 不允许"忘了传"就静默变成某个值。
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    x_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 最近一次失败原因（已脱敏）。publishing / publish_unknown 的说明也在这里，
    # 但状态不动。
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    draft: Mapped["Draft"] = relationship(back_populates="published_posts")
