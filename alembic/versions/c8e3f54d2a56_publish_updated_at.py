"""publish updated_at

Revision ID: c8e3f54d2a56
Revises: 0b17346696c6
Create Date: 2026-09-21 20:30:34.047524

published_post 增加 updated_at：人工裁决需要一个"这条记录从什么时候起停在这个状态"
的锚点。重试路径是 UPDATE 复用同一行，created_at 不会刷新，因此没有这一列就无法
判断一个 publish_unknown 是 5 分钟前还是 3 周前留下的。

注意：本文件是 `alembic revision -m`（不带 --autogenerate）生成的空模板后手工填写，
**刻意不走 autogenerate**——LangGraph 的 4 张 checkpoint 表
（checkpoints / checkpoint_writes / checkpoint_blobs / checkpoint_migrations）
不在 Base.metadata 里，autogenerate 每次都把它们误判为"已删除"并生成 drop_table，
那会删掉全部会话断点。详见上一个 migration 的模块注释。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e3f54d2a56'
down_revision: Union[str, None] = '0b17346696c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 让已有行立刻有值；不撤掉——updated_at 本来就应该在插入时
    # 自动取 now()，与 CreatedAtMixin 的处理一致。
    op.add_column(
        'published_post',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column('published_post', 'updated_at')
