import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

#: 提案的确定性 key 上的唯一约束。repositories 用 ON CONFLICT DO NOTHING 做
#: "一条提案只裁决一次"，必须按名字指定冲突目标，因此名字是接口的一部分
#: （同 published_post.UQ_AGENT_RUN_ID / post_metrics_snapshot.UQ_X_POST_OBSERVED）。
UQ_LEARNING_PROPOSAL_KEY = "uq_learning_proposal_proposal_key"


class LearningProposalRecord(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """**一条被人工裁决过的学习提案**（Phase 5D 工作单第三十二、三十三节）。

    ## 这张表只装"裁决"，不装"提案"

    提案本身是**派生出来的**：给定同一批快照与同一个观察窗口，`generate_proposals()`
    每次得到逐位相同的一组提案。把派生结果存起来，等于制造第二个真相来源——
    数据一更新，表里的提案就成了"当时算过的、现在不再成立的"那一种历史包袱。

    本表装的是**派生不出来的那一样东西**：**人在某个时刻对某条提案做了什么决定**。
    这条信息不存在于任何快照、任何报告、任何 checkpoint 里，丢了就永远丢了。

    所以：

        未裁决的提案   → 读的时候现算，`status='proposed'`，**不落库**
        已裁决的提案   → 裁决那一刻写一行，此后只读

    于是"提案"与"对提案的裁决"是两件事，各存在它该存在的地方。全表只插入、
    不更新（`reviewed_at` 与 `status` 一旦写下就不再改）——同 post_metrics_snapshot
    的追加纪律。改主意不是改这一行，而是等下一次观察产生一条新提案。

    ## 为什么裁决要存**提案内容**，而不是只存一个 id

    只存 `proposal_key` 的话，这条记录的含义会随着底层数据漂移：三个月后有人
    打开审计，看到"approve 了 proposal:hook_type@24-72h"，却再也查不到当时
    建议的到底是什么（那批快照可能已被清理，或配方早已换版）。所以这里把
    `target` / `current_value` / `proposed_value` / `rationale` / `evidence_ids` /
    `sample_count` / `observation_window` / `limitations` 一起存下来，让一行记录
    **自证**：它说的是哪一条建议、依据了哪几条证据、样本多少、边界在哪。
    这正好是工作单第三十四节允许保存的三样：结构化提案、证据引用、理由。

    ## 版本号是"这次裁决过期了没有"的判据

    `current_recipe_version` / `proposed_recipe_version` 记的是**裁决那一刻**
    两个配方的版本（Phase 5D 第十三节）。它们不驱动任何自动切换——本表的存在
    不改变任何一次运行的行为。它们的用处只有一个：事后能回答
    "这条批准是在哪一版配方上做的"。当前配方已经不是当时那一版时，读取路径
    会把这条裁决**展示为 `expired`**（见 `app/services/admin.py`）——注意
    "展示为"：本表里那一行仍然是 `approved`，过期是**读出来的判断**，
    不是一次系统写入。

    ## 刻意没有的列

    - **没有** `prompt` / `raw_completion` / 任何模型原文（工作单第三十四节）。
      提案的措辞是确定性模板生成的，存进库的只有 `note`——那是人写的。
    - **没有** `proposal_score` / `priority` / `ranking`（工作单第十七节）。
      给提案打分需要一套没人校准过的权重，那个分数会被当成"该先做哪个"的依据。
    - **没有** `applied_at` / `applied_by`。本阶段**没有**任何应用路径
      （工作单第三十五节），一个恒为 NULL 的列会被读成"还没应用"，
      而事实是"不存在应用这件事"。

    ## 状态取值

    `status` 只写 `approved` / `rejected` 两种（`app/learning/schema.py::ProposalStatus`）。
    刻意不写 `proposed`（未裁决的不落库）也不写 `expired`（过期是读时的判断，
    不是历史事实）。列上没有 CHECK 约束：取值由 Python 侧的封闭枚举把关，
    而枚举改动不该顺带要求一次迁移。
    """

    __tablename__ = "learning_proposal"
    __table_args__ = (UniqueConstraint("proposal_key", name=UQ_LEARNING_PROPOSAL_KEY),)

    #: 确定性提案 id（形如 `proposal:hook_type@24-72h`，见 `app/learning/proposal.py`）。
    #: 唯一键就是它：同一条提案被裁决两次时，第二次被 `ON CONFLICT DO NOTHING` 挡住。
    proposal_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    current_value: Mapped[str] = mapped_column(String(128), nullable=False)
    proposed_value: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 证据 id 列表。**非空**：没有证据的提案在生成阶段就被丢弃了，
    #: 所以这里不该出现空列表——真出现了，说明写入路径漏了一道校验。
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: 提案的理由，**确定性模板生成的原文**（工作单第三十四节允许存"理由"）。
    #: 它必须跟着行一起存：一条裁决在半年后可能已经**不再被生成**（数据变了），
    #: 那时它是唯一还说得清"当初为什么提这条"的东西。
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    #: 提案自带的边界说明（观察性比较 / 不合成单一分数 / 只建议尝试 / 缺哪一维）。
    #: 同上：它不随下游数据变化，是提案内容的一部分，不是可以从别处再算一遍的派生值。
    limitations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: 帖子数（不是快照数，同 5C 的分析单位）。
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_window: Mapped[str] = mapped_column(String(32), nullable=False)
    current_recipe_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proposed_recipe_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 'approved' | 'rejected'。见类文档。
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 人做出决定的时刻。与 `created_at`（行写入时刻）分开记：两者通常只差几毫秒，
    #: 但它们是两个不同的事实——"这行是什么时候建的"与"这个决定是什么时候做的"。
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 人工批注。**唯一一段由人写的自由文本**，可空。
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
