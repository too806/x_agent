"""Agent 预算与「下一步能做什么」的判定。

工作单第三节把预算列为硬要求（"绝不能无限循环"）。这里的关键设计判断是：

> **预算不能交给 LLM 自觉遵守。**

一个被要求"注意别做太多"的模型，在它认为研究还不够时仍然会继续研究——而预算存在的
意义恰恰是在模型判断失误时**仍然能停下来**。所以本模块全是纯函数、无 IO、无随机，
Planner 只负责"想做什么"，"能不能做"由这里说了算。

`allowed_actions` 同时承担两件事：
- 告诉 Planner 哪些动作今天可行（写进 prompt，减少无效决策）；
- 在 Planner 拿到一个不可行的决策时，**确定性地**决定改做什么。

两个职责用同一个函数，就不会出现"prompt 里说可以、实际被拦下"的漂移。
"""

from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, Field

from app.agent.schemas import AgentAction
from app.config import settings


class AgentBudget(BaseModel):
    """一次 Agent 运行的硬预算（config/agent.yaml，可配置）。"""

    max_steps: int = Field(default=12, ge=1)
    max_research_calls: int = Field(default=4, ge=0)
    max_candidates: int = Field(default=3, ge=1)
    max_revisions: int = Field(default=2, ge=0)
    # 一轮生成产出几条候选。与 max_candidates 分开：前者是 Writer 的写法宽度，
    # 后者是整轮运行允许存在的候选总数。
    candidates_per_round: int = Field(default=3, ge=1)


def load_budget(path: str | None = None) -> AgentBudget:
    file_path = Path(path or settings.agent_budget_path)
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    return AgentBudget(**data)


# 回退顺序：从"离成品最近"到"最接近从头再来"，`finish` 兜底。
#
# 顺序本身就是策略，所以写成常量而不是散在一串 if 里。两条性质必须保持：
#   1. `finish` 在最后——它是唯一永远可行的动作，因此回退一定能终止；
#   2. 越靠前的动作越"不可逆地推进流程"，越靠后的越接近重做已做过的事。
#
# 这条顺序解决的是一个具体问题：模型想 research，但研究预算已经用完了。
# 此时正确答案不是"结束"，而是"用手上的材料往下走"（extract_evidence），
# 所以 finish 不能排在前面。
FALLBACK_ORDER: tuple[AgentAction, ...] = (
    AgentAction.REVISE,
    AgentAction.GENERATE_CANDIDATES,
    AgentAction.SELECT_ANGLE,
    AgentAction.GENERATE_ANGLES,
    AgentAction.EXTRACT_EVIDENCE,
    AgentAction.DEEPEN_RESEARCH,
    AgentAction.RESEARCH_TOPIC,
    AgentAction.DISCOVER_TOPICS,
    AgentAction.FINISH,
)


def _counter(state: Mapping[str, Any], key: str) -> int:
    """读一个计数字段。缺失或类型不对一律当 0。

    State 是 checkpoint 里的数据，可能来自旧版本或被外部写入，
    因此这里不假设类型正确——一个坏值不该让整条运行 500。
    """
    value = state.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _filled(state: Mapping[str, Any], key: str) -> bool:
    """字段是否"已经有内容"。None、空串、空列表都算没有。"""
    value = state.get(key)
    if value is None:
        return False
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value) > 0
    return True


def revision_pending(state: Mapping[str, Any]) -> bool:
    """Critic 是否刚刚要求改稿。

    `revise` 只在这一刻可行：Critic 说 accept 或 reject 时都不该改稿——
    accept 之后改稿会推翻刚刚被认可的东西，reject 之后改稿则是无视结论。
    """
    critic = state.get("critic_result")
    return getattr(critic, "decision", None) == "revise"


def allowed_actions(state: Mapping[str, Any], budget: AgentBudget) -> tuple[AgentAction, ...]:
    """当前状态下 Planner 可以选的动作集合。

    返回值用元组而不是集合：调用方要按 `FALLBACK_ORDER` 取"第一个可行的"，
    而集合的迭代顺序在 Python 里虽然稳定但不是契约。
    """
    research = _counter(state, "research_count")
    revisions = _counter(state, "revision_count")
    candidates = state.get("candidates") or []

    allowed: list[AgentAction] = []

    if _filled(state, "candidate_topics") and research < budget.max_research_calls:
        allowed.append(AgentAction.RESEARCH_TOPIC)

    if (
        _filled(state, "selected_topic")
        and _filled(state, "research_item_ids")
        and research < budget.max_research_calls
    ):
        allowed.append(AgentAction.DEEPEN_RESEARCH)

    if _filled(state, "research_item_ids"):
        allowed.append(AgentAction.EXTRACT_EVIDENCE)

    if _filled(state, "evidence_pack"):
        allowed.append(AgentAction.GENERATE_ANGLES)

    # 已经产出候选之后不再允许重新选 angle：候选池是满的，重新选角度并不能
    # 生成对应的新候选（generate_candidates 被 max_candidates 挡住），
    # 只会把 state 改成"角度与候选不匹配"。要换角度得先改稿或结束。
    if _filled(state, "angles") and not candidates:
        allowed.append(AgentAction.SELECT_ANGLE)

    if _filled(state, "selected_angle") and len(candidates) < budget.max_candidates:
        allowed.append(AgentAction.GENERATE_CANDIDATES)

    # 只在"候选被写出来之后还没被评价过"时允许评价。
    # 没有这条，Planner 可以反复选 criticize 而每次都在评价同一批候选。
    if candidates and _counter(state, "writer_epoch") > _counter(state, "critic_epoch"):
        allowed.append(AgentAction.CRITICIZE)

    if revision_pending(state) and revisions < budget.max_revisions:
        allowed.append(AgentAction.REVISE)

    # 先把 discovery 的一次性条件说清楚：还没发现过候选选题时才能发现。
    # discovery 也计入研究预算——否则"发现不出东西"会变成一个可以无限重试的动作。
    if not _filled(state, "candidate_topics") and research < budget.max_research_calls:
        allowed.append(AgentAction.DISCOVER_TOPICS)

    # finish 永远可行：它是唯一的出口，也是回退一定终止的保证。
    allowed.append(AgentAction.FINISH)

    return tuple(allowed)


def pick_fallback(state: Mapping[str, Any], budget: AgentBudget) -> AgentAction:
    """模型选了一个不可行的动作时，确定性地改选一个。

    返回的一定是 `allowed_actions` 里的成员：`finish` 恒可行，而它在
    `FALLBACK_ORDER` 末尾，所以最坏情况就是结束这一轮。
    """
    allowed = set(allowed_actions(state, budget))
    for action in FALLBACK_ORDER:
        if action in allowed:
            return action
    return AgentAction.FINISH  # pragma: no cover - finish 恒在 allowed 里


def steps_exhausted(state: Mapping[str, Any], budget: AgentBudget) -> bool:
    """步数是否已经用尽。

    单独抽出来，是因为它要能**在调用 LLM 之前**判定：预算用尽时不该再花一次
    模型调用，也不该给模型一个"其实已经没有选择"的决策机会。
    """
    return _counter(state, "step_count") >= budget.max_steps
