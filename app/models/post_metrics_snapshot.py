import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

# 唯一约束名。repositories 用 ON CONFLICT DO NOTHING 做幂等重跑，必须按名字
# 指定冲突目标，因此名字是接口的一部分（同 published_post.UQ_AGENT_RUN_ID）。
UQ_X_POST_OBSERVED = "uq_post_metrics_snapshot_x_post_observed"


class PostMetricsSnapshot(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """某个已发布帖子在**某一个时刻**的真实 X 公开指标。

    ## 为什么是"追加"而不是"更新 published_post 上的四个数字"

    因为变化本身就是反馈信息：

        Day 0  likes = 3
        Day 1  likes = 27
        Day 7  likes = 204

    只保留最新值，等于把"这条帖子是怎么长起来的"丢掉，只留下一个终点。
    所以本表只插入、不更新——没有 update 路径，表里每一行都是当时的事实。

    ## 为什么值得落库，而不是留在 checkpoint

    checkpoint 装的是"这次运行决定了什么"（style_recipe / selected_angle 都是
    运行期决策，刻意不落库）。而本表装的是**外部世界对我们过去行为的事实**：
    它必须比这次 Run 活得更久，也必须能跨 Run 被读取——"我上次发的东西后来
    怎么样了"这个问题，天然不属于任何单次运行。

    ## 没有存的东西

    `engagement_total` / 各种 rate 一律**不存**。它们是本表四列计数的纯函数，
    逐位可复算；落成列只会多出第二个真相来源，然后某天悄悄漂掉（同 style_sample
    迁移里的理由）。

    唯一的例外是 `hours_since_publish`：它不是本行四列的函数，还要 join 回
    published_post 才知道发布时刻，所以在这里算好存下来。

    ## 时间语义（沿用三字段纪律，不发明第四种）

    - `published_at` 在 published_post 上：**来源给定**的发布时刻，绝不猜。
    - `observed_at` 在本表：**我们**抓取的时刻，一次抓取一个值，整批共用。
    - `created_at` 在本表：数据库插入时刻，只有 DB 层用。

    ## NULL 一律表示"没拿到"，不表示 0

    likes / replies / reposts / quotes 可以为 NULL：X 没返回这个字段，或者这条
    帖子根本没被 X 返回（删除、受限）。此时写 0 会把"不知道"读成"没人互动"。
    impressions / bookmarks v1 从不写入，恒为 NULL——它们需要 user-context 且
    当前没有抓取路径；列先留着，将来有来源时不必再迁移。
    """

    __tablename__ = "post_metrics_snapshot"
    # 唯一键必须是 (x_post_id, observed_at)，**不能只有 observed_at**：
    # 一次批量抓取会给多条不同帖子盖上同一个 observed_at，用 observed_at 单独
    # 做唯一键会把同一时刻的其他帖子全部判成冲突丢掉。
    __table_args__ = (
        UniqueConstraint("x_post_id", "observed_at", name=UQ_X_POST_OBSERVED),
        Index("ix_post_metrics_snapshot_x_post_id", "x_post_id"),
        Index("ix_post_metrics_snapshot_published_post_id", "published_post_id"),
    )

    published_post_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("published_post.id", ondelete="CASCADE"), nullable=False
    )
    # 从 published_post.x_post_id 复制而来。查 X 用的是它，且它不随内部行 id 变化。
    x_post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 我们抓取的时刻。一次批量抓取共用一个值（同 XService 给整批研究结果盖一个
    # observed_at 的做法）。
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 发布后过了多少小时。CLI 只对 x_post_id 与 published_at 都非空的帖子抓取，
    # 因此这里非空——"不知道发布了多久"的帖子根本不进入本表，而不是存一个 NULL 年龄。
    hours_since_publish: Mapped[float] = mapped_column(Float, nullable=False)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replies: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reposts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quotes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v1 恒为 NULL。见类文档。
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bookmarks: Mapped[int | None] = mapped_column(Integer, nullable=True)
