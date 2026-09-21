"""phase4 publish state

Revision ID: 0b17346696c6
Revises: 72970ca1affb
Create Date: 2026-09-21 16:32:44.601136

published_post 从"纯记录表"变成发布状态机的载体：
- status：publishing / published / failed，本表独占发布语义，不借用 agent_run.status
- last_error：最近一次失败原因（含"结果未知"的说明）
- UNIQUE(agent_run_id)：一个 Run 最多一条发布记录，作为并发的第二道防线

注意：本文件由 autogenerate 生成后**手工清理过**。LangGraph 的 4 张 checkpoint 表
（checkpoints / checkpoint_writes / checkpoint_blobs / checkpoint_migrations）不在
Base.metadata 里，autogenerate 每次都把它们误判为“已删除”并生成 drop_table——
那会删掉全部会话断点。以后重新生成 migration 时同样要剔掉这几条。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0b17346696c6'
down_revision: Union[str, None] = '72970ca1affb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 只为让 ADD COLUMN 在非空表上也能通过，加完立刻撤掉：
    # 状态语义不允许存在"没指定就默认成某个值"的隐式路径。
    op.add_column(
        'published_post',
        sa.Column('status', sa.String(length=32), nullable=False, server_default='published'),
    )
    op.alter_column('published_post', 'status', server_default=None)
    op.add_column('published_post', sa.Column('last_error', sa.Text(), nullable=True))
    op.create_unique_constraint('uq_published_post_agent_run_id', 'published_post', ['agent_run_id'])


def downgrade() -> None:
    op.drop_constraint('uq_published_post_agent_run_id', 'published_post', type_='unique')
    op.drop_column('published_post', 'last_error')
    op.drop_column('published_post', 'status')
