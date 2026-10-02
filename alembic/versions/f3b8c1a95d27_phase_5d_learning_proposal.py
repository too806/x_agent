"""phase 5d learning proposal

Revision ID: f3b8c1a95d27
Revises: e5f1a9c4d20b
Create Date: 2026-10-02 18:00:00.000000

Phase 5D 的全部 schema 变更：**一张新表，没有任何列加在已有的表上**。

    learning_proposal —— 一条被人工裁决过的学习提案

## 为什么这张表配得上进数据库（沿用 5B.2 / 5B.4 的逐条论证）

**1. 为什么 checkpoint 装不下？**
checkpoint 绑定一次 Run，而一条提案的裁决与 Run 无关——提案看的是**一批**
已发布帖子在某个观察窗口下的表现，跨许多次 Run。更要紧的是：裁决是一条
**审计记录**。"三个月前谁批准了什么"必须比任何一次运行活得更久，
而 checkpoint 会随 GC 消失。

**2. 为什么不能复用已有表？**
- 不能放在 `review` 上：那张表记的是**草稿**的人工审核（approve/edit/reject），
  对象是"这一次要发出去的正文"。本表的对象是"未来要不要换个写法"，
  两者连被审核的东西都不是同一类。
- 不能放在 `agent_decision` 上：那是**Agent 自己**在一次运行里做的决策，
  而本表记的是**人**对一条建议的裁决。混在一起会让"谁决定的"失去答案。
- 不能复用 `post_metrics_snapshot`：那是观察事实，本表是决定。观察可以重采，
  决定丢了就是丢了。

**3. 为什么它属于长期数据？**
裁决时刻不可复现：人在某一版配方、某一份样本上看过这条建议并做了决定。
事后可以重新算出一条新提案，但**算不出当时那个决定**。

## 为什么存的是"已裁决"的提案，而不是全部提案

提案本身是纯函数派生的（同 5B.4 里"报告是观察的纯函数"），存它等于制造第二个
真相来源。派生不出来的只有**裁决**，所以只有裁决落库：

    未裁决 → 读时现算，status='proposed'，不落库
    已裁决 → 写一行，此后只读（全表只插入，没有 UPDATE 路径）

行的内容包含提案本身的快照（target / current_value / proposed_value /
rationale / evidence_ids / sample_count / observation_window / limitations），
让一行记录自证——三个月后底层快照被清理了，这行仍然说得出"当时建议的是什么、
依据了什么、边界在哪"。

## 什么**没有**建

- **没有** prompt / raw_completion 列（工作单第三十四节）。落库的文本只有两种，
  都是刻意保留的：`rationale` / `limitations` 是**确定性模板**生成的提案原文
  （第三十四节允许保存"结构化提案、证据引用、理由"），`note` 是**人**写的批注。
  没有任何模型生成的自由文本。
- **没有** proposal_score / priority / ranking（工作单第十七节）。打分需要一套
  没人校准过的权重，而那个分数会被读成"该先做哪个"的依据。
- **没有** applied_at / applied_by。本阶段**没有**应用路径（工作单第三十五节）；
  一个恒为 NULL 的列会被读成"还没应用"，而事实是"不存在应用这件事"。
- **没有** status 的 CHECK 约束。取值由 Python 侧的封闭枚举把关，
  而枚举演进不该顺带要求一次迁移（同 5B.4 对计数的处理）。
- **没有** proposed 状态的存量行，也**没有** expired 行：过期是**读的时候**
  比配方版本比出来的判断（工作单第三十二节），不是一条历史事实。
  表里的行永远是 approved / rejected。

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
revision: str = 'f3b8c1a95d27'
down_revision: Union[str, None] = 'e5f1a9c4d20b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'learning_proposal',
        # 确定性提案 id（形如 proposal:hook_type@24-72h）。唯一键就是它：
        # 同一条提案被裁决两次时，第二次由 ON CONFLICT DO NOTHING 挡住，
        # 于是审计记录里不会有第二行相互矛盾的决定。
        sa.Column('proposal_key', sa.String(length=255), nullable=False),
        sa.Column('target', sa.String(length=64), nullable=False),
        sa.Column('current_value', sa.String(length=128), nullable=False),
        sa.Column('proposed_value', sa.String(length=128), nullable=False),
        # 证据 id 列表。**非空**：没有证据的提案在生成阶段就被丢弃了。
        sa.Column('evidence_ids', postgresql.JSONB(), nullable=False),
        # 提案的理由（确定性模板生成的原文）。跟着行一起存：一条裁决半年后
        # 可能已经不再被生成，那时它是唯一还说得清"当初为什么提这条"的东西。
        sa.Column('rationale', sa.Text(), nullable=False),
        # 提案自带的边界说明。同上——它是提案内容的一部分，不是可再算一遍的派生值。
        sa.Column('limitations', postgresql.JSONB(), nullable=False),
        # 帖子数而不是快照数——同 5C 的分析单位（一条帖子的多条快照是重复观察）。
        sa.Column('sample_count', sa.Integer(), nullable=False),
        sa.Column('observation_window', sa.String(length=32), nullable=False),
        # 裁决**那一刻**的两个配方版本。可空：目标不在 StyleRecipe 上、
        # 或当时取不到基准配方时，它就是一个诚实的 NULL，而不是一个编出来的版本号。
        sa.Column('current_recipe_version', sa.String(length=32), nullable=True),
        sa.Column('proposed_recipe_version', sa.String(length=32), nullable=True),
        # 'approved' | 'rejected'。见模块注释：proposed / expired 不落库。
        sa.Column('status', sa.String(length=32), nullable=False),
        # 人做出决定的时刻。与 created_at（行写入时刻）分开记。
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=False),
        # 唯一一段由人写的自由文本，可空。
        sa.Column('note', sa.String(length=1000), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('proposal_key', name='uq_learning_proposal_proposal_key'),
    )


def downgrade() -> None:
    op.drop_table('learning_proposal')
