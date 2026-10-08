import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun
    from app.models.draft import Draft

#: 自动审阅是在哪种模式下做出的判断。
#: `shadow` = 只记录、不动作（人仍然照常审）；`live` = 通过就会真的走发布。
#:
#: 必须记下来：影子期的记录和正式期的记录混在一起读，"通过率"就同时混了
#: "机器敢不敢"和"机器真的做了没有"两件事，而这两件事的比较对象不同。
MODE_SHADOW = "shadow"
MODE_LIVE = "live"

VERDICT_APPROVE = "approve"
VERDICT_REJECT = "reject"

#: 一个 Draft 只允许有一条自动判断。重复 tick（或 tick 与手动端点撞上）时
#: 由 ON CONFLICT DO NOTHING 挡住，先写的那次说了算。
UQ_AUTO_REVIEW_DRAFT = "uq_auto_review_draft"


class AutoReview(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """自动审阅者对一条草稿的**判断记录**。

    这张表只在自动发布这件事上有意义，但它不是日志——它是影子模式唯一的产出物。

    ## 为什么是独立一张表，而不是 review 表上的一列

    `review.decided_by` 回答的是"**最终那个决定**是谁做的"，一张草稿上永远只有
    一个决定。而影子模式要的是**两个独立判断的对照**：自动审阅者说了什么、
    人又说了什么。把机器人的意见塞进人那一行，等于让两个判断互相覆盖——
    而"它们一致吗"正是影子模式存在的全部理由。

    ## 为什么它属于长期数据

    影子转正式的门槛是一个可测的混淆矩阵（见 `GET /api/admin/auto-publish/status`），
    而这个矩阵只能从"自动判断 ⋈ 人的决定"配对算出来。人的决定在 review 表里，
    自动判断就必须有地方落。两者缺一，门槛就只能靠印象。

    ## 存了什么、没存什么

    存：判断结论（`verdict`）、逐条理由与逐项判据的原始取值（`reasons` / `checks`）、
    当时看到的 Critic 分数、以及是否真的动作了（`acted`）。

    `checks` 存**判据的取值本身**而不只是"过没过"，因为转正式前要回答
    "哪条判据在误拦"——只知道"被拒了"是回答不了的。

    不存正文：正文在 `draft` 表里，这里再存一份就是第二个真相来源。
    不存模型名 / prompt / 原始输出：本层**没有任何 LLM 调用**。
    """

    __tablename__ = "auto_review"
    __table_args__ = (UniqueConstraint("draft_id", name=UQ_AUTO_REVIEW_DRAFT),)

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("draft.id", ondelete="CASCADE"), nullable=False
    )
    # 见 MODE_SHADOW / MODE_LIVE。
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # 见 VERDICT_APPROVE / VERDICT_REJECT。
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    # 给人看的理由（中文短句，逐条），以及判据的原始取值。都非空：
    # 一条没有理由的判断在界面上没法审、在统计里没法归因。
    reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # 当时那条交付候选的 Critic 分数。可空：交付候选可能压根没被 Critic 评价过，
    # 而那种情况下判据是拒绝——NULL 是"没有评价"，不是"0 分"。
    critic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 这条内容**可能真的被发出去了**——正式模式下发布确认成功，或结果未知
    # （请求可能已送达）。确定没出去的失败留 False。
    # 影子模式恒为 False。存下来是为了让"机器说通过"和"机器做了"能分开数，
    # 所以它不是"尝试过发布"，也不是"判据通过了"：一次 402 的失败两条都不是。
    acted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    agent_run: Mapped["AgentRun"] = relationship()
    draft: Mapped["Draft"] = relationship()
