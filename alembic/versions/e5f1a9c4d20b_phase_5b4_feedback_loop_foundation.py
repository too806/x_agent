"""phase 5b.4 feedback loop foundation

Revision ID: e5f1a9c4d20b
Revises: c3d8a45e7f21
Create Date: 2026-10-02 12:00:00.000000

Phase 5B.4 的全部 schema 变更：**一张新表，没有任何列加在已有的表上**。

    post_metrics_snapshot —— 我们自己的帖子在某个时刻的真实 X 公开指标

## 为什么这张表配得上进数据库（同 5B.2 的逐条论证）

**1. 为什么 checkpoint 装不下？**
checkpoint 装的是"这次运行决定了什么"——它是运行期状态，Run 结束就只是历史。
本表装的是**外部世界对我们过去行为的事实**：读了它的可能是三个月后的另一次
Run（"我以前发的东西后来怎么样"）。这个问题的生命周期天然长于任何一次运行，
而 checkpoint 会随 GC 消失。

**2. 为什么不能复用已有表？**
- 不能放在 `published_post` 上：那是"一条帖子一行"的当前结局，只能留一个最新
  数字，而本阶段要的正是**时间序列**（Day 0 / Day 1 / Day 7 的变化本身就是
  反馈信息）。
- 不能复用 `research_item` / `style_sample` 上的互动列：那两处的计数描述的是
  **别人发的**帖子（外部材料 / 语料样本）。语义上"我们自己的帖子表现如何"是
  另一个对象，混用会让"这条帖子的互动"有两种含义。

**3. 为什么它属于长期数据？**
捕获时刻是不可复现的——过了那个时间点，X 不会再把当时的计数还给我们。因此
每一行都是一次性的事实记录，只能追加。

## 什么**没有**建

- **没有**派生指标列（`engagement_total` / `reply_rate` / …）。它们是四列计数的
  纯函数，逐位可复算；落成列等于制造第二个会漂移的真相来源。唯一存下来的派生值
  是 `hours_since_publish`——它还要 join 回 published_post 才能算，不是本行的函数。
- **没有** `feedback_signal` / `feedback_report` 表。信号与报告是**读取时**从
  快照确定性算出来的观察，属于派生视图，不属于长期事实。存它们等于把"当时算出的
  结论"固化成另一个真相来源。
- **没有** impressions / bookmarks 的采集路径。两列先建（可空），v1 恒为 NULL；
  有了 user-context 抓取路径时不必再迁移。写 0 会把"没拿到"读成"没人看"。

## 生成方式

`alembic revision -m`（不带 --autogenerate）生成空模板后手工填写。
**刻意不走 autogenerate**：LangGraph 的 4 张 checkpoint 表不在 Base.metadata 里，
autogenerate 会把它们误判为"已删除"并生成 drop_table，那会删掉全部会话断点。
详见 c8e3f54d2a56 的模块注释。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f1a9c4d20b'
down_revision: Union[str, None] = 'c3d8a45e7f21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'post_metrics_snapshot',
        sa.Column('published_post_id', sa.Uuid(), nullable=False),
        sa.Column('x_post_id', sa.String(length=255), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('hours_since_publish', sa.Float(), nullable=False),
        # 四个公开计数可空：X 没返回这个字段，或者这条帖子根本没被 X 返回。
        # 写成 0 会把"不知道"读成"没人互动"。
        sa.Column('likes', sa.Integer(), nullable=True),
        sa.Column('replies', sa.Integer(), nullable=True),
        sa.Column('reposts', sa.Integer(), nullable=True),
        sa.Column('quotes', sa.Integer(), nullable=True),
        # v1 恒为 NULL，见模块注释。
        sa.Column('impressions', sa.Integer(), nullable=True),
        sa.Column('bookmarks', sa.Integer(), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['published_post_id'], ['published_post.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        # 唯一键是 (x_post_id, observed_at)，**不是 observed_at 单独**：
        # 一次批量抓取会给多条帖子盖同一个 observed_at，单独用 observed_at
        # 会把同一时刻的其他帖子全部判成冲突丢掉。
        sa.UniqueConstraint(
            'x_post_id', 'observed_at', name='uq_post_metrics_snapshot_x_post_observed'
        ),
    )
    op.create_index(
        'ix_post_metrics_snapshot_x_post_id', 'post_metrics_snapshot', ['x_post_id']
    )
    op.create_index(
        'ix_post_metrics_snapshot_published_post_id',
        'post_metrics_snapshot',
        ['published_post_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_post_metrics_snapshot_published_post_id',
        table_name='post_metrics_snapshot',
    )
    op.drop_index(
        'ix_post_metrics_snapshot_x_post_id', table_name='post_metrics_snapshot'
    )
    op.drop_table('post_metrics_snapshot')
