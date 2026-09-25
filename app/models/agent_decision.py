import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun

# 唯一约束名。与 published_post 的 UQ_AGENT_RUN_ID 同理，名字是接口的一部分：
# 仓库层要按名字判断"是不是同一步被写了两次"。
UQ_RUN_STEP = "uq_agent_decision_run_step"


class AgentDecision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Agent 每一步决策的持久记录（Phase 5A 的 Decision Trace）。

    与 `app/agent/schemas.py::AgentDecision` **同名但不同物**，刻意不互相 import：
    那边是"模型想做什么"（LLM 输出的契约），这边是"这一决策被记下来了"（数据库行）。
    Planner 节点是唯一同时接触两者的地方，落库在那里一处完成。

    只存**摘要**，不存完整 prompt：prompt 里含全部研究正文，体积大且没有审计价值，
    而人真正要看的是"这一步做了什么、为什么、结果如何"。
    """

    __tablename__ = "agent_decision"
    # 一个 Run 的每一步最多一行。step 从 1 开始单调递增，所以这条约束同时也
    # 保证了"同一步被写两次"在 schema 层就不可能发生——比在应用层做去重更可靠。
    __table_args__ = (UniqueConstraint("agent_run_id", "step", name=UQ_RUN_STEP),)

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    # AgentAction 的值（discover_topics / research_topic / ... / finish）。
    # 存字符串而不是枚举：数据库层不该绑定 Python 枚举，加一个新动作也就不需要迁移。
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 作用于哪个对象：topic 文本 / angle_id / candidate_id。没有对象时为空。
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 决策**之前**的状态摘要（还剩多少预算、已有什么材料）——判断这一步合不合理，
    # 关键就在当时手上有什么。
    input_summary: Mapped[str] = mapped_column(Text, nullable=False)
    # 这一步实际产出了什么。执行完成后由节点回填。
    result_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
