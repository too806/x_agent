"""Planner 的决策契约与预算判定（工作单第四、三节）。

这里全是纯函数级的验证，不碰数据库、不碰 Graph：预算与"下一步能不能做"是
**确定性逻辑**，它必须能被单独证明，而不是靠一条端到端用例的绿灯来间接担保。
"""

import pytest
from pydantic import ValidationError

from app.agent import budget as budget_mod
from app.agent.budget import (
    WRAPUP_ALLOWANCE,
    AgentBudget,
    allowed_actions,
    load_budget,
    pick_fallback,
    ready_to_finish,
    steps_exhausted,
    steps_hard_exhausted,
)
from app.agent.prompts import ACTION_HINTS, format_allowed, load_prompt
from app.agent.schemas import AgentAction, AgentDecision
from app.agent.tools.base import complete_json
from app.services.llm import LLMError
from fakes import PROMPT_MARKERS, ScriptedLLMService

BUDGET = AgentBudget(
    max_steps=12, max_research_calls=4, max_candidates=3, max_revisions=2
)


# --------------------------------------------------------------------------
# AgentDecision：结构化输出契约
# --------------------------------------------------------------------------


def test_decision_accepts_a_legal_action():
    decision = AgentDecision.model_validate_json(
        '{"action": "research_topic", "reason": "材料还不够", "target": "某个选题"}'
    )
    assert decision.action is AgentAction.RESEARCH_TOPIC
    assert decision.target == "某个选题"
    assert decision.arguments == {}


@pytest.mark.parametrize("action", ["do_magic", "research", "FINISH", ""])
def test_decision_rejects_unknown_action(action):
    """自由文本决定不了路由：action 不在枚举里就直接被拒。

    大小写敏感是刻意的——`FINISH` 与 `finish` 都被接受的话，prompt 里那句
    "逐字照抄"就失去了意义，而枚举的价值恰恰在于取值集合是封闭的。
    """
    with pytest.raises(ValidationError):
        AgentDecision.model_validate_json(f'{{"action": "{action}", "reason": "x"}}')


def test_decision_requires_a_non_empty_reason():
    with pytest.raises(ValidationError):
        AgentDecision.model_validate_json('{"action": "finish", "reason": ""}')


async def test_invalid_decision_surfaces_as_llm_error():
    """非法输出与"LLM 调用失败"同类（上游故障 → 502），不是代码缺陷。"""
    llm = ScriptedLLMService(decisions=['{"action": "do_magic", "reason": "x"}'])
    with pytest.raises(LLMError):
        await complete_json(llm, "你是这个内容 Agent 的 Planner", AgentDecision)


# --------------------------------------------------------------------------
# allowed_actions：能做什么由确定性逻辑说了算
# --------------------------------------------------------------------------


def test_nothing_is_possible_before_discovery_but_discovery():
    """开局只有一条路：去发现选题。

    注意 finish **不在**里面——没有正文的收尾必然失败，把它摆成选项只是给
    Planner 一次注定失败的选择。
    """
    assert allowed_actions({}, BUDGET) == (AgentAction.DISCOVER_TOPICS,)


def test_finish_is_blocked_until_candidates_are_critiqued():
    """写出来的东西必须被评价过才能收尾（工作单第十一至十三节的链路）。"""
    written = {"candidates": [object()], "writer_epoch": 1, "critic_epoch": 0}
    assert AgentAction.FINISH not in allowed_actions(written, BUDGET)

    critiqued = {**written, "critic_epoch": 1}
    assert AgentAction.FINISH in allowed_actions(critiqued, BUDGET)


def test_revision_reopens_the_need_for_a_critique():
    """改完必须重新评价："改完就算过"是做不到的。"""
    # critic_result 是必须的：没有它 `revise` 本来就不可行，
    # 那样测的就不是"改稿预算"，而是一个恒真的条件。
    state = {
        "candidates": [object()],
        "writer_epoch": 2,
        "critic_epoch": 1,
        "critic_result": _revise_verdict(),
    }
    assert AgentAction.FINISH not in allowed_actions(state, BUDGET)
    assert AgentAction.CRITICIZE in allowed_actions(state, BUDGET)
    assert AgentAction.REVISE in allowed_actions(state, BUDGET)  # 还有额度

    # revise 的额度用完也不影响"必须评价"这一条——预算管的是改稿，不是检查
    spent = {**state, "revision_count": BUDGET.max_revisions}
    assert AgentAction.REVISE not in allowed_actions(spent, BUDGET)
    assert AgentAction.CRITICIZE in allowed_actions(spent, BUDGET)


def test_research_budget_closes_the_research_actions():
    state = {
        "candidate_topics": [object()],
        "selected_topic": "题目",
        "research_item_ids": ["id-1"],
        "research_count": BUDGET.max_research_calls,
    }
    allowed = allowed_actions(state, BUDGET)
    assert AgentAction.RESEARCH_TOPIC not in allowed
    assert AgentAction.DEEPEN_RESEARCH not in allowed
    # 但手上的材料照样能往证据推进——预算用尽不等于流程结束
    assert AgentAction.EXTRACT_EVIDENCE in allowed


def test_angles_are_not_generated_from_an_empty_evidence_pack():
    """证据包存在但一条事实都没有时，不能形成观点（工作单第九节）。"""
    from app.agent.schemas import EvidencePack

    empty = {"evidence_pack": EvidencePack(facts=[], unknowns=["不知道"])}
    assert AgentAction.GENERATE_ANGLES not in allowed_actions(empty, BUDGET)

    with_facts = {"evidence_pack": EvidencePack.model_validate({"facts": [{"fact": "x", "source_ids": ["s"]}]})}
    assert AgentAction.GENERATE_ANGLES in allowed_actions(with_facts, BUDGET)


def test_ready_to_finish_needs_actual_content():
    assert ready_to_finish({}) is False
    assert ready_to_finish({"candidates": [object()], "writer_epoch": 1}) is False
    assert ready_to_finish({"candidates": [object()], "writer_epoch": 1, "critic_epoch": 1}) is True


# --------------------------------------------------------------------------
# 回退：模型选了不可行的动作之后
# --------------------------------------------------------------------------


def test_fallback_picks_the_most_advanced_available_action():
    """预算用尽又还在研究时，正确答案不是"结束"，而是"用手上的材料往下走"。"""
    state = {
        "candidate_topics": [object()],
        "selected_topic": "题目",
        "research_item_ids": ["id-1"],
        "research_count": BUDGET.max_research_calls,
    }
    assert pick_fallback(state, BUDGET) is AgentAction.EXTRACT_EVIDENCE


def test_fallback_prefers_revising_over_everything_else():
    state = {
        "candidates": [object()],
        "critic_result": _revise_verdict(),
        "writer_epoch": 1,
        "critic_epoch": 1,
    }
    assert pick_fallback(state, BUDGET) is AgentAction.REVISE


def test_fallback_order_always_ends_at_finish():
    """回退一定能终止：finish 在最后，所以最坏情况就是结束这一轮。"""
    assert budget_mod.FALLBACK_ORDER[-1] is AgentAction.FINISH
    assert set(budget_mod.FALLBACK_ORDER) == set(AgentAction)


def test_fallback_still_returns_finish_when_nothing_is_possible():
    """任何动作都不可行时的最后出口。不是正常路径，但必须是终止的。"""
    assert pick_fallback({}, AgentBudget(max_research_calls=0)) is AgentAction.FINISH


def _revise_verdict():
    from app.agent.schemas import CriticResult

    return CriticResult(candidate_id="c1", decision="revise", score=4.0)


# --------------------------------------------------------------------------
# 预算本身
# --------------------------------------------------------------------------


def test_budget_defaults_come_from_config():
    """工作单第三节的四个数字必须可配置，且默认值就是要求的那组。"""
    loaded = load_budget()
    assert loaded.max_steps == 12
    assert loaded.max_research_calls == 4
    assert loaded.max_candidates == 3
    assert loaded.max_revisions == 2


def test_steps_exhausted_has_a_hard_ceiling():
    budget = AgentBudget(max_steps=3)
    assert steps_exhausted({"step_count": 2}, budget) is False
    assert steps_exhausted({"step_count": 3}, budget) is True

    # 收尾余量之内仍允许走完最后几步（不调用 LLM），超过就硬停
    assert steps_hard_exhausted({"step_count": 3 + WRAPUP_ALLOWANCE - 1}, budget) is False
    assert steps_hard_exhausted({"step_count": 3 + WRAPUP_ALLOWANCE}, budget) is True


def test_counters_ignore_broken_values():
    """state 来自 checkpoint，可能被外部写坏；一个坏值不该让整条运行 500。"""
    assert budget_mod.counter({"step_count": "12"}, "step_count") == 0
    assert budget_mod.counter({"step_count": True}, "step_count") == 0
    assert budget_mod.counter({"step_count": 3}, "step_count") == 3


# --------------------------------------------------------------------------
# prompt 与枚举的一致性
# --------------------------------------------------------------------------


def test_action_hints_cover_every_action():
    """枚举里加了动作却忘了写说明，Planner 会看到一个空白的选项。"""
    assert set(ACTION_HINTS) == set(AgentAction)


def test_allowed_list_is_rendered_from_the_same_source():
    rendered = format_allowed((AgentAction.FINISH, AgentAction.REVISE))
    assert "- finish：" in rendered
    assert "- revise：" in rendered


@pytest.mark.parametrize("kind,marker", sorted(PROMPT_MARKERS.items()))
def test_agent_prompt_markers_match_templates(kind, marker):
    """假 LLM 靠特征句识别是哪一步的 prompt。

    这条测试把那份耦合变成显式的：改了模板却忘了同步特征句，
    失败在这里（一眼看懂），而不是在某个 Agent 用例里报"无法识别这是哪个 prompt"。
    """
    assert marker in load_prompt(kind)
