"""phase 5a agent brain

Revision ID: a1f0c7d3b5e2
Revises: c8e3f54d2a56
Create Date: 2026-09-26 10:12:00.000000

Phase 5A 的**全部** schema 变更，刻意只有三件事：

1. agent_run.goal —— Agent 的输入是目标而不是题目。
2. agent_run.topic 放宽为可空 —— 主题由 Agent 自己发现，创建 Run 时还不知道。
3. agent_decision 新表 —— Decision Trace（每一步决策的持久记录）。

没有第四件。EvidencePack / Angle / Candidate / CriticResult 一律只活在 LangGraph
checkpoint 里，经 aget_state 暴露——与既有的 guardrail_* / final_content 完全同一套
机制。给它们建表等于把 checkpoint 已经承担的事再抄一份，两处必然漂移。

`topic` 放宽可空**不改动任何既有语义**：旧行全部有值，`goal` 全部为 NULL，
因此所有历史 Run 与既有代码路径的行为逐字不变。

注意：本文件是 `alembic revision -m`（不带 --autogenerate）生成的空模板后手工填写，
**刻意不走 autogenerate**——LangGraph 的 4 张 checkpoint 表
（checkpoints / checkpoint_writes / checkpoint_blobs / checkpoint_migrations）
不在 Base.metadata 里，autogenerate 每次都把它们误判为"已删除"并生成 drop_table，
那会删掉全部会话断点。详见 c8e3f54d2a56 的模块注释。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f0c7d3b5e2'
down_revision: Union[str, None] = 'c8e3f54d2a56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_run', sa.Column('goal', sa.Text(), nullable=True))
    # 放宽而不是重建：PostgreSQL 的 DROP NOT NULL 是纯元数据操作，
    # 不重写表、不触碰任何一行数据。
    op.alter_column('agent_run', 'topic', existing_type=sa.Text(), nullable=True)

    op.create_table(
        'agent_decision',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('agent_run_id', sa.Uuid(), nullable=False),
        sa.Column('step', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('target', sa.String(length=255), nullable=True),
        sa.Column('input_summary', sa.Text(), nullable=False),
        sa.Column('result_summary', sa.Text(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['agent_run_id'], ['agent_run.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # step 单调 + 唯一：让"同一步被写两次"在 schema 层就不可能。
        sa.UniqueConstraint('agent_run_id', 'step', name='uq_agent_decision_run_step'),
    )


def downgrade() -> None:
    op.drop_table('agent_decision')
    # downgrade 会把 topic 重新收紧成 NOT NULL。Phase 5A 之后创建的行可能
    # topic 为空（Agent 还没选出主题就失败了），此时这一步会失败——这是正确的：
    # 回滚 schema 而留下不满足旧约束的数据，比直接报错更糟。
    op.alter_column('agent_run', 'topic', existing_type=sa.Text(), nullable=False)
    op.drop_column('agent_run', 'goal')
