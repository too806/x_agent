"""phase 5b.2 style intelligence

Revision ID: c3d8a45e7f21
Revises: b7c2e94f1a03
Create Date: 2026-09-30 11:00:00.000000

Phase 5B.2 的**全部** schema 变更：三张新表，**没有任何列加在已有的表上**。

    tracked_account  —— 人工指定的样本来源账号
    style_sample     —— 经过筛选、进入 Style Corpus 的真实帖子
    style_profile    —— 从一批样本算出的统计特征

## 为什么这三张表配得上进数据库（工作单第三十三节要求逐条论证）

对每一张，三个问题都要有答案：

**1. 为什么 checkpoint 装不下？**
checkpoint 绑定一次 Run：它的生命周期是"这次运行"，而不是"这个语料"。
Style Corpus 要在**许多次运行之间**复用——今天的 Run 与三个月后的 Run 引用
同一版 Profile，并且要能回答"当时用的是哪一版"。放进 checkpoint 等于每次
运行重算一遍，而重算就得在请求路径上拿到全部样本原文，正是工作单禁止的
"大段原文进入运行期"。

**2. 为什么不能复用 `research_item`？**
`research_item.agent_run_id` 是 NOT NULL + ON DELETE CASCADE：删掉一次 Run
会连带删掉它采到的全部样本，而 corpus 的生命周期必须长于任何一次 Run。
另外 `research_item` 刻意没有 UNIQUE(platform_post_id)（多轮检索取回同一条
帖子在趋势打分里就该算多条材料），corpus 恰好相反——它需要去重键，否则一条
爆款会被数很多遍，把 Profile 拽向它。语义也不同：同一张帖子可以是"这次运行
取回的材料"却不是"被接纳进语料的样本"（互动不够、是转发、正文不完整）。

**3. 为什么它属于长期语料？**
样本是外部来源的事实记录，账号集合是人划定的研究范围，Profile 是可复用的
统计结论。三者都不随某次 Run 的结束而失效，也不随历史清理而消失。

## 什么**没有**建

- **没有** `candidate` / `evidence` / `angle` / `style_recipe` 表。它们是单次
  Run 的工作状态，继续只活在 checkpoint 里（机制与 `guardrail_*` /
  `final_content` 一致，见 a1f0c7d3b5e2）。给它们建表等于把 checkpoint 已经
  承担的事抄一份，两处必然漂移。特别是 **StyleRecipe 刻意不落库**：
  它是"这次运行选了什么结构"，是运行期决策，不是语料资产。
- **没有**显式特征列（长度、emoji 率、英文比例…）在 `style_sample` 上。
  那些全部能从 `content` 重算，且重算逐位相同（见 app/style/features.py 的
  纯函数契约）。存一份等于制造一个将来会漂移的第二来源。

## 生成方式

`alembic revision -m`（不带 --autogenerate）生成空模板后手工填写。
**刻意不走 autogenerate**：LangGraph 的 4 张 checkpoint 表
（checkpoints / checkpoint_writes / checkpoint_blobs / checkpoint_migrations）
不在 Base.metadata 里，autogenerate 每次都把它们误判为"已删除"并生成
drop_table，那会删掉全部会话断点。详见 c8e3f54d2a56 的模块注释。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3d8a45e7f21'
down_revision: Union[str, None] = 'b7c2e94f1a03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tracked_account',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('username', sa.String(length=255), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=64), nullable=True),
        sa.Column('source_type', sa.String(length=64), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # 一个账号一行。重名账号会让"这条样本属于谁"变得没有答案。
        sa.UniqueConstraint('username', name='uq_tracked_account_username'),
    )

    op.create_table(
        'style_sample',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tracked_account_id', sa.Uuid(), nullable=False),
        sa.Column('platform_post_id', sa.String(length=255), nullable=False),
        sa.Column('author_username', sa.String(length=255), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('likes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('reposts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('replies', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('quote_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_repost', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_link_only', sa.Boolean(), nullable=False, server_default=sa.false()),
        # 与 is_repost 同一类事实（来自 X 的 referenced_tweets），
        # 而 referenced_tweets 是**混装**列表：只看"非空"会把回复标成转发。
        sa.Column('is_reply', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('included', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('exclusion_reason', sa.String(length=255), nullable=True),
        sa.Column('content_type', sa.String(length=64), nullable=True),
        sa.Column('structure_type', sa.String(length=64), nullable=True),
        sa.Column('hook_type', sa.String(length=64), nullable=True),
        sa.Column('has_claim', sa.Boolean(), nullable=True),
        sa.Column('has_evidence', sa.Boolean(), nullable=True),
        sa.Column('has_counterpoint', sa.Boolean(), nullable=True),
        sa.Column('has_question', sa.Boolean(), nullable=True),
        sa.Column('has_list', sa.Boolean(), nullable=True),
        sa.Column('has_number', sa.Boolean(), nullable=True),
        sa.Column('has_conclusion', sa.Boolean(), nullable=True),
        sa.Column('annotation_model', sa.String(length=128), nullable=True),
        sa.Column('annotation_version', sa.String(length=64), nullable=True),
        sa.Column('annotated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('annotation_confidence', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ['tracked_account_id'], ['tracked_account.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        # corpus 的去重键 —— research_item 刻意没有的那一个，理由见模块 docstring。
        sa.UniqueConstraint('platform_post_id', name='uq_style_sample_platform_post_id'),
    )
    op.create_index(
        'ix_style_sample_tracked_account_id', 'style_sample', ['tracked_account_id']
    )
    op.create_index('ix_style_sample_included', 'style_sample', ['included'])

    op.create_table(
        'style_profile',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('tracked_account_id', sa.Uuid(), nullable=True),
        sa.Column('sample_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sample_window_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sample_window_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('computed_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('feature_version', sa.String(length=32), nullable=False),
        sa.Column('taxonomy_version', sa.String(length=32), nullable=False),
        sa.Column('median_length', sa.Float(), nullable=True),
        sa.Column('p25_length', sa.Float(), nullable=True),
        sa.Column('p75_length', sa.Float(), nullable=True),
        sa.Column('median_paragraphs', sa.Float(), nullable=True),
        sa.Column('emoji_density', sa.Float(), nullable=True),
        sa.Column('question_density', sa.Float(), nullable=True),
        sa.Column('number_density', sa.Float(), nullable=True),
        sa.Column('english_ratio', sa.Float(), nullable=True),
        sa.Column('mean_sentence_count', sa.Float(), nullable=True),
        sa.Column('structure_distribution', postgresql.JSONB(),
                  nullable=False, server_default='{}'),
        sa.Column('hook_distribution', postgresql.JSONB(),
                  nullable=False, server_default='{}'),
        sa.Column('opening_distribution', postgresql.JSONB(),
                  nullable=False, server_default='{}'),
        sa.Column('closing_distribution', postgresql.JSONB(),
                  nullable=False, server_default='{}'),
        sa.Column('sentence_length_distribution', postgresql.JSONB(),
                  nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ['tracked_account_id'], ['tracked_account.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_style_profile_scope', 'style_profile', ['scope'])


def downgrade() -> None:
    op.drop_index('ix_style_profile_scope', table_name='style_profile')
    op.drop_table('style_profile')
    op.drop_index('ix_style_sample_included', table_name='style_sample')
    op.drop_index('ix_style_sample_tracked_account_id', table_name='style_sample')
    op.drop_table('style_sample')
    op.drop_table('tracked_account')
