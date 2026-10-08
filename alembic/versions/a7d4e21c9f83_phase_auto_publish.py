"""phase auto publish

Revision ID: a7d4e21c9f83
Revises: f3b8c1a95d27
Create Date: 2026-10-06 20:00:00.000000

自动发布的全部 schema 变更：**两张新列 + 一张新表**。

    agent_run.origin      谁发起了这个 Run（manual / scheduled）
    review.decided_by     这个决定是人做的还是自动审阅者做的
    auto_review           自动审阅者对一条草稿的判断记录

## 第一次给已有的表加列

Phase 5C / 5D 的迁移注释都以"**没有给任何已有表加列**"为纪律，理由每次都写得很清楚：
一旦把派生值复制成列，就多出一个真相来源，然后某天悄悄漂掉。

这两列**不是派生值，也无法从别处推出来**，所以那条纪律不适用于它们：

- `agent_run.origin`：范围查询（当日额度、影子一致率、回收残留 claim）都要按它筛。
  没有它就只能靠"goal 等于配置里的 goal"去猜来源，而人可以提交同一个 goal。
  一个会把人的 Run 算成机器的推断，比多一列危险得多。
- `review.decided_by`：发布路径不带这个信息（人和机器最终都走同一个
  `PublishService`），事后没有任何办法从别的表反推"这条帖子是谁放行的"。

两列都带 `server_default`，因此既有行**不需要回填**就已经自洽——这一列出现之前，
所有 Run 确实都是人发起的，所有决定确实都是人做的。默认值在这里不是占位符，
它是对历史的准确陈述。

## 为什么 auto_review 配得上进数据库（沿用 5B.2 / 5B.4 / 5D 的逐条论证）

**1. 为什么 checkpoint 装不下？**
判断的对象是一条 Draft，而影子模式要拿它和**人的决定**配对。人的决定在 review 表里
且比任何一次 Run 活得久；判断要能与之相配，就必须同样持久。

**2. 为什么不能复用已有表？**
- 不能放在 `review` 上：一张草稿永远只有**一个**最终决定，而影子模式要的是
  **两个独立判断的对照**（机器说了什么、人又说了什么）。塞进同一行就是互相覆盖，
  而"它们一致吗"正是这一层存在的全部理由。
- 不能放在 `agent_decision` 上：那张表记的是 **Agent 自己**在一次运行里做的决策，
  是运行中的过程。本表记的是一个**外部的审阅者**对运行产物的判断，而且它要在
  Run 结束之后仍然可读。混在一起会让"谁决定的"失去答案。

**3. 为什么它属于长期数据？**
影子转正式的门槛是一个混淆矩阵，只能由"自动判断 ⋈ 人的决定"算出来。
判断丢了，门槛就只能是印象——而印象正是这套设计要避免的东西。

## 什么**没有**建

- **没有** status 的 CHECK 约束：取值由 Python 侧的封闭常量把关，
  枚举演进不该顺带要求一次迁移（同 5B.4 对计数的处理）。
- **没有**正文列：正文在 draft 表里，这里再存一份就是第二个真相来源。
- **没有**模型名 / prompt / raw completion：本层没有任何 LLM 调用。
- **没有**把 `origin` / `decided_by` 做成外键或枚举类型：它们是标签不是实体，
  PostgreSQL 的 ENUM 每次加值都要一次迁移。

## 生成方式

`alembic revision -m`（不带 --autogenerate）生成空模板后**手工填写**。
**刻意不走 autogenerate**：LangGraph 的 4 张 checkpoint 表不在 Base.metadata 里，
autogenerate 会把它们误判为"已删除"并生成 drop_table，那会删掉全部会话断点。
详见 c8e3f54d2a56 的模块注释。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a7d4e21c9f83'
down_revision: Union[str, None] = 'f3b8c1a95d27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 见模块注释"第一次给已有的表加列"：server_default 让既有行无需回填即自洽。
    op.add_column(
        'agent_run',
        sa.Column(
            'origin',
            sa.String(length=16),
            nullable=False,
            server_default='manual',
        ),
    )
    op.add_column(
        'review',
        sa.Column(
            'decided_by',
            sa.String(length=16),
            nullable=False,
            server_default='human',
        ),
    )

    op.create_table(
        'auto_review',
        sa.Column('agent_run_id', sa.Uuid(), nullable=False),
        sa.Column('draft_id', sa.Uuid(), nullable=False),
        # 'shadow' | 'live'。见 app/models/auto_review.py 的 MODE_*。
        sa.Column('mode', sa.String(length=16), nullable=False),
        # 'approve' | 'reject'。
        sa.Column('verdict', sa.String(length=16), nullable=False),
        # 给人工看的理由与判据的原始取值。**非空**：一条没有理由的判断
        # 在界面上没法审、在统计里没法归因。
        # 存 checks 的取值本身而不只是"过没过"，是因为转正式前要回答
        # "哪条判据在误拦"——只知道"被拒了"是回答不了的。
        sa.Column('reasons', postgresql.JSONB(), nullable=False),
        sa.Column('checks', postgresql.JSONB(), nullable=False),
        # 可空：交付候选可能压根没被 Critic 评价过（此时判据是拒绝）。
        # NULL 是"没有评价"，不是"0 分"。
        sa.Column('critic_score', sa.Float(), nullable=True),
        # 这次判断有没有真的导致动作。影子模式恒为 False。
        sa.Column('acted', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['agent_run_id'], ['agent_run.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['draft_id'], ['draft.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # 一个 Draft 只允许一条自动判断：重复 tick 由 ON CONFLICT DO NOTHING
        # 按这个名字挡住，先写的那次说了算。
        sa.UniqueConstraint('draft_id', name='uq_auto_review_draft'),
    )
    op.create_index(
        'ix_auto_review_agent_run_id', 'auto_review', ['agent_run_id']
    )


def downgrade() -> None:
    op.drop_index('ix_auto_review_agent_run_id', table_name='auto_review')
    op.drop_table('auto_review')
    op.drop_column('review', 'decided_by')
    op.drop_column('agent_run', 'origin')
