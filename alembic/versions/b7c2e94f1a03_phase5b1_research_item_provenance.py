"""phase 5b.1 research item provenance

Revision ID: b7c2e94f1a03
Revises: a1f0c7d3b5e2
Create Date: 2026-09-29 10:40:00.000000

Phase 5B.1 的**全部** schema 变更，只有两列，都加在 `research_item` 上：

1. `quote_count` —— X 的 `public_metrics` 一直返回这一项，映射时被丢掉了。
   trend 的 engagement 口径要求四个公开计数齐全（引用是"讨论被放大"的信号，
   与转发、点赞不是同一件事），缺一项就算不完整。
2. `observed_at` —— 观察时间，与 `published_at`（来源时间）、`created_at`
   （记录时间）区分开。

**没有第三件事。** 具体说，这次刻意**不建**这些表：

    candidate_topic / evidence / angle / candidate_content

`candidate_topics` / `evidence_pack` / `angles` / `candidates` / `critic_results`
是 Agent 单次 Run 的工作状态，继续只活在 LangGraph checkpoint 里，机制与
Phase 5A 的 `guardrail_*` / `final_content` 完全相同（见 a1f0c7d3b5e2 的说明）。
给它们建表等于把 checkpoint 已经承担的事再抄一份，两处必然漂移。

`research_item` 为什么可以加列：它**不是** Agent 的工作状态，而是从 X 取回的
外部来源数据。它的列就是"我们观察到了什么来源"的记录。跨 Run 复用、
控制台展示、事后核对某条 claim 的出处，都要靠它。

`observed_at` 可空且**不回填**：历史行没有这个信息。用一个编造的时刻把它们
填满，会让控制台把三天前的讨论显示成刚刚发生——比留空更糟。UI 侧把 NULL
显示成 `—`。

注意：本文件是 `alembic revision -m`（不带 --autogenerate）生成的空模板后手工填写，
**刻意不走 autogenerate** —— LangGraph 的 4 张 checkpoint 表
（checkpoints / checkpoint_writes / checkpoint_blobs / checkpoint_migrations）
不在 Base.metadata 里，autogenerate 每次都把它们误判为"已删除"并生成 drop_table，
那会删掉全部会话断点。详见 c8e3f54d2a56 的模块注释。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c2e94f1a03'
down_revision: Union[str, None] = 'a1f0c7d3b5e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 非空 + server_default '0'：已有行必须有值，而 0 在语义上是正确的
    # （那一列本来就没记过，而"没记到引用数"与"引用数是 0"在这一列上
    # 无法区分 —— 选择 0 是因为它对下游是中性值：不会伪造出互动信号）。
    op.add_column(
        'research_item',
        sa.Column('quote_count', sa.Integer(), nullable=False, server_default='0'),
    )
    # 可空：历史行没有观察时间，NULL 才是诚实的表示。
    op.add_column(
        'research_item',
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('research_item', 'observed_at')
    op.drop_column('research_item', 'quote_count')
