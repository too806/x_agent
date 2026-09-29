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
    AgentAction.CRITICIZE,
    AgentAction.GENERATE_CANDIDATES,
    AgentAction.SELECT_ANGLE,
    AgentAction.GENERATE_ANGLES,
    AgentAction.EXTRACT_EVIDENCE,
    AgentAction.DEEPEN_RESEARCH,
    AgentAction.SELECT_TOPIC,
    # DISCOVER_TOPICS 排在 RESEARCH_TOPIC 前面：开局两者都可行时（还没有任何候选），
    # 回退应当选**制造选项**的那一个。RESEARCH_TOPIC 在没有候选时只能用目标原文
    # 去搜（见 tools/research.py::_choose_topic），那是"跳过发现"的退化路径——
    # 一个都不认识就往下走，等于把本阶段最要紧的"选什么值得写"整个跳掉。
    AgentAction.DISCOVER_TOPICS,
    AgentAction.RESEARCH_TOPIC,
    AgentAction.FINISH,
)


def counter(state: Mapping[str, Any], key: str) -> int:
    """读一个计数字段。缺失或类型不对一律当 0。

    State 是 checkpoint 里的数据，可能来自旧版本或被外部写入，
    因此这里不假设类型正确——一个坏值不该让整条运行 500。
    """
    value = state.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def filled(state: Mapping[str, Any], key: str) -> bool:
    """字段是否"已经有内容"。None、空串、空列表都算没有。"""
    value = state.get(key)
    if value is None:
        return False
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value) > 0
    return True


def has_evidence(state: Mapping[str, Any]) -> bool:
    """是否已经有**可用**的证据。

    不能只看 `evidence_pack` 是否为空对象：抽取阶段可能把全部事实都筛掉了
    （每条事实的来源都追溯不到真实材料时，工具会把它们丢掉，见 evidence 工具）。
    那种情况下证据包存在但没有任何事实，此时允许生成 Angle 等于允许无凭据地
    形成观点——正是工作单第九节要禁止的事。
    """
    return bool(getattr(state.get("evidence_pack"), "facts", None))


def revision_pending(state: Mapping[str, Any]) -> bool:
    """Critic 是否刚刚要求改稿。

    `revise` 只在这一刻可行：Critic 说 accept 或 reject 时都不该改稿——
    accept 之后改稿会推翻刚刚被认可的东西，reject 之后改稿则是无视结论。
    """
    critic = state.get("critic_result")
    return getattr(critic, "decision", None) == "revise"


def ready_to_finish(state: Mapping[str, Any]) -> bool:
    """现在收尾有没有意义。

    两个条件缺一不可：

    1. **写出过候选**。没有正文的 finish 必然失败（finalize 会抛 AgentIncompleteError），
       所以不该把它当成一个可选项摆给 Planner 看——那只是给了它一次注定失败的选择。
    2. **写出来的东西已经被评价过**（`writer_epoch <= critic_epoch`）。工作单的链路是
       生成 → 评审 → 改稿，跳过评审等于把一条没人检查过的稿子直接交给人工审核：
       人看到的会是"Agent 认为这条没问题"，而实际上根本没人看过。

    第 2 条同时管住了改稿循环：revise 会推进 writer_epoch，于是改完必须重新评审，
    "改完就算过"是做不到的。而评审次数不受 `max_revisions` 限制——预算管的是改稿，
    不是检查。
    """
    if not state.get("candidates"):
        return False
    return counter(state, "writer_epoch") <= counter(state, "critic_epoch")


def allowed_actions(state: Mapping[str, Any], budget: AgentBudget) -> tuple[AgentAction, ...]:
    """当前状态下 Planner 可以选的动作集合。

    返回值用元组而不是集合：调用方要按 `FALLBACK_ORDER` 取"第一个可行的"，
    而集合的迭代顺序在 Python 里虽然稳定但不是契约。
    """
    research = counter(state, "research_count")
    revisions = counter(state, "revision_count")
    candidates = state.get("candidates") or []

    allowed: list[AgentAction] = []

    # 选定候选选题。
    #
    # `not filled(selected_topic)` 这个条件让 SELECT_TOPIC 自动成为**一次性**的：
    # 选完之后它不再可行，Planner 换不了题。这是刻意的——换题会让已经取回的
    # 材料、已经形成的证据与新的选题对不上，而 state 里没有任何字段能表达
    # "这些材料属于上一个题"。要换题就得新开一次运行。
    if filled(state, "candidate_topics") and not filled(state, "selected_topic"):
        allowed.append(AgentAction.SELECT_TOPIC)

    # research_topic **不再要求先有过候选选题**（Phase 5B.1 第八节）。
    #
    # 理由：Goal 决定 Agent 从哪里开始。"帮我研究 OpenAI 最近发布的某功能"
    # 这种目标里题目已经给定了，强行先做一轮 discovery 只会多花一次 X 调用、
    # 多花一步预算，还可能把用户指定的题目换成一个"趋势更好"的。
    # 没有候选时 research 工具用目标文本兜底（见 tools/research.py::_choose_topic）。
    #
    # 它**只**受研究预算约束，不因为 selected_topic 已经有值而被关掉：
    # "研究了 A 发现材料不对，改研究 B"是合法的自主行为（候选集里本来就有多个），
    # 关掉它等于把"研究了一个不合适的题"变成一条死路。
    if research < budget.max_research_calls:
        allowed.append(AgentAction.RESEARCH_TOPIC)

    if (
        filled(state, "selected_topic")
        and filled(state, "research_item_ids")
        and research < budget.max_research_calls
    ):
        allowed.append(AgentAction.DEEPEN_RESEARCH)

    if filled(state, "research_item_ids"):
        allowed.append(AgentAction.EXTRACT_EVIDENCE)

    # 生成 Angle 的意义在于"它还能被选中、进而变成候选"。候选一旦写出来，
    # 选 Angle 就被挡在下面那条（候选池里的正文对应的是当时那个角度），
    # 于是新生成的 Angle 永远选不上——纯粹白做。
    #
    # 这不只是一个洁癖问题：`has_evidence` 之后 generate_angles 原本一直可行，
    # 而它在 FALLBACK_ORDER 里排在 finish 前面，于是"候选池已满 + 最近一轮评审
    # 已完成"的收尾会反复重新生成 Angle，直到把 WRAPUP_ALLOWANCE 耗光、
    # 最后被强制 finish（RULE-013 记录的真实场景）。条件与下面的 SELECT_ANGLE
    # 对齐，两者一起表达同一件事：候选写出来之后，角度就定了。
    if has_evidence(state) and not candidates:
        allowed.append(AgentAction.GENERATE_ANGLES)

    # 已经产出候选之后不再允许重新选 angle：候选池是满的，重新选角度并不能
    # 生成对应的新候选（generate_candidates 被 max_candidates 挡住），
    # 只会把 state 改成"角度与候选不匹配"。要换角度得先改稿或结束。
    if filled(state, "angles") and not candidates:
        allowed.append(AgentAction.SELECT_ANGLE)

    if filled(state, "selected_angle") and len(candidates) < budget.max_candidates:
        allowed.append(AgentAction.GENERATE_CANDIDATES)

    # 只在"候选被写出来之后还没被评价过"时允许评价。
    # 没有这条，Planner 可以反复选 criticize 而每次都在评价同一批候选。
    if candidates and counter(state, "writer_epoch") > counter(state, "critic_epoch"):
        allowed.append(AgentAction.CRITICIZE)

    if revision_pending(state) and revisions < budget.max_revisions:
        allowed.append(AgentAction.REVISE)

    # 先把 discovery 的一次性条件说清楚：还没发现过候选选题时才能发现。
    # discovery 也计入研究预算——否则"发现不出东西"会变成一个可以无限重试的动作。
    if not filled(state, "candidate_topics") and research < budget.max_research_calls:
        allowed.append(AgentAction.DISCOVER_TOPICS)

    if ready_to_finish(state):
        allowed.append(AgentAction.FINISH)

    return tuple(allowed)


def run_is_complete(state: Mapping[str, Any], budget: AgentBudget, allowed: set[AgentAction]) -> bool:
    """成品已就绪，且已经没有动作能再改变结局。

    两个条件：

    1. `ready_to_finish`——有正文，且这一版正文已经被评审过。
    2. **候选池已满**——`generate_candidates` 被 `max_candidates` 挡住，而 Angle
       那边在候选写出来之后也已经封口（见 `allowed_actions`）。继续产出是
       **做不了**，不是"不该做"。

    有待办时不算完成：Critic 还没评（`writer_epoch > critic_epoch`）或刚要求改稿
    （`revision_pending`）都得先做正事。前者已被 `ready_to_finish` 排除，后者
    在这里再挡一次。
    """
    if not ready_to_finish(state):
        return False
    if len(state.get("candidates") or []) < budget.max_candidates:
        return False
    return AgentAction.REVISE not in allowed and AgentAction.CRITICIZE not in allowed


def pick_fallback(state: Mapping[str, Any], budget: AgentBudget) -> AgentAction:
    """模型选了一个不可行的动作时，确定性地改选一个。

    返回的一定是 `allowed_actions` 里的成员——除非那个集合是空的。集合为空意味着
    当前状态下没有任何动作能推进流程（例如候选写不出来、研究预算又用尽），
    此时返回 `finish` 作为**最后的出口**：宁可让这次运行以"没有产出"收场，
    也不能让图停在一个没有出边的节点上。这是终止性的兜底，不是正常路径。
    """
    allowed = set(allowed_actions(state, budget))
    # 成品已就绪时，finish 优先于排在它前面的那些"重复劳动"。
    #
    # 不能无条件把 finish 提到最前：研究预算刚用尽时还没有成品，那时候的正确
    # 答案恰恰是"用手上的材料往下走"（见 FALLBACK_ORDER 的说明）。所以只在
    # `run_is_complete` 成立时提前——那意味着剩下的动作只会重做已完成的工作。
    if AgentAction.FINISH in allowed and run_is_complete(state, budget, allowed):
        return AgentAction.FINISH
    for action in FALLBACK_ORDER:
        if action in allowed:
            return action
    return AgentAction.FINISH


def steps_exhausted(state: Mapping[str, Any], budget: AgentBudget) -> bool:
    """决策步数是否已经用尽（LLM 不再参与）。

    单独抽出来，是因为它要能**在调用 LLM 之前**判定：预算用尽时不该再花一次
    模型调用，也不该给模型一个"其实已经没有选择"的决策机会。
    """
    return counter(state, "step_count") >= budget.max_steps


# 预算用尽后允许的**收尾**步数上界。
#
# 为什么用尽之后还要允许再走几步：工作单第三节要求"预算耗尽 → 停止 Agent →
# 安全兜底 / 人工审核"，而不是"直接什么都没有就结束"。一个把 12 步全花在研究上的
# Agent，此时手上通常已经有材料甚至观点了，正确的收尾是按 FALLBACK_ORDER 一路推到
# 出稿，而不是丢掉全部劳动。
#
# 这不会变成"无限循环"，因为它有**两重**上界：
#   1. 收尾不调用 LLM（`pick_fallback` 是纯函数），所以不会再有模型的自由发挥；
#   2. 收尾动作严格按 FALLBACK_ORDER 单调推进，而该顺序以 `finish` 结尾。
# 这个常量是第三重、也是最笨的一重保险：万一前两条推理有错，硬停。
WRAPUP_ALLOWANCE = 6


def steps_hard_exhausted(state: Mapping[str, Any], budget: AgentBudget) -> bool:
    """连收尾余量也用完了。到这一步只可能 finish。"""
    return counter(state, "step_count") >= budget.max_steps + WRAPUP_ALLOWANCE
