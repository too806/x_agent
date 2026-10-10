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
from app.agent.schemas import (
    AgentAction,
    AgentDecision,
    CandidateTopic,
    EvidenceFact,
    EvidencePack,
    TopicQualification,
)
from app.agent.tools.base import complete_json
from app.services.llm import LLMError
from fakes import PROMPT_MARKERS, ScriptedLLMService

BUDGET = AgentBudget(
    max_steps=12, max_research_calls=4, max_candidates=3, max_revisions=2
)


def selectable_topic(
    topic_id: str = "t1",
    *,
    score: float = 80.0,
    relevance: bool | None = True,
) -> CandidateTopic:
    """一个**可被选中**的候选：达标（qualified）且与目标相关。

    `allowed_actions` 里的几个条件现在判的是"有没有一个可被选中的候选"
    （工作单第二、三节），所以预算测试必须拿真实的候选对象当输入——
    `object()` 在那套判据下等于"一个都不达标"，测出来的是另一件事。
    """
    return CandidateTopic(
        topic_id=topic_id,
        title=f"候选选题 {topic_id}",
        trend_score=score,
        qualification=TopicQualification.QUALIFIED,
        goal_relevance=relevance,
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
    """非法输出与"LLM 调用失败"同类（上游故障 → 502），不是代码缺陷。

    脚本给两条，是因为 `complete_json` 会为重写结构化输出**再调一次**（见
    `MAX_STRUCTURED_OUTPUT_ATTEMPTS`）。两条都非法，重试也用完，才落到 LLMError。
    """
    bad = '{"action": "do_magic", "reason": "x"}'
    llm = ScriptedLLMService(decisions=[bad, bad])
    with pytest.raises(LLMError):
        await complete_json(llm, "你是这个内容 Agent 的 Planner", AgentDecision)
    assert llm.planner_calls() == 2


# --------------------------------------------------------------------------
# allowed_actions：能做什么由确定性逻辑说了算
# --------------------------------------------------------------------------


def test_opening_moves_are_exactly_discovery_or_directed_research():
    """开局只有两条路：去发现选题，或者直接研究目标里已经指定的题目。

    第二条是 Phase 5B.1 加的（工作单第八节"Goal 决定 Agent 从哪里开始"）：
    用户说"研究 OpenAI 最近发布的某功能"时，题目已经给定了，逼 Agent 先做一轮
    发现只会多花一次 X 调用、还可能把用户指定的题目换掉。

    注意 finish **不在**里面——没有正文的收尾必然失败，把它摆成选项只是给
    Planner 一次注定失败的选择。
    """
    assert allowed_actions({}, BUDGET) == (
        AgentAction.RESEARCH_TOPIC,
        AgentAction.DISCOVER_TOPICS,
    )


def test_select_topic_is_available_only_between_discovery_and_selection():
    """选题目是**一次性**的：选完之后它不再可行。

    换题会让已经取回的材料、已经形成的证据与新的选题对不上，而 state 里没有
    任何字段能表达"这些材料属于上一个题"。要换题就得新开一次运行。
    """
    discovered = {"candidate_topics": [selectable_topic()]}
    assert AgentAction.SELECT_TOPIC in allowed_actions(discovered, BUDGET)

    chosen = {**discovered, "selected_topic": "题目"}
    assert AgentAction.SELECT_TOPIC not in allowed_actions(chosen, BUDGET)


def test_select_topic_needs_a_qualified_and_relevant_candidate():
    """有候选**不等于**可以选：达标与相关缺一不可（工作单第二、三节）。

    Run 20ab360c 的目标是「今天想关注香港」，两个候选一个达标分数都没有、
    材料里也没有出现过「香港」，但 `allowed_actions` 照旧把 select_topic
    摆了出来，于是"分最高的那个"被选中，写成了一篇与目标无关的分析。
    """
    weak = {
        "candidate_topics": [
            selectable_topic(score=40.0),
        ]
    }
    # 把达标这一条改掉：分数再高也不能选。
    weak["candidate_topics"][0] = weak["candidate_topics"][0].model_copy(
        update={"qualification": TopicQualification.WEAK_SUPPORT}
    )
    assert AgentAction.SELECT_TOPIC not in allowed_actions(weak, BUDGET)

    irrelevant = {
        "candidate_topics": [selectable_topic(relevance=False)],
    }
    assert AgentAction.SELECT_TOPIC not in allowed_actions(irrelevant, BUDGET)

    # `None` = 本次没有判定相关性，**放行**：没判定不等于判定为不相关。
    undecided = {"candidate_topics": [selectable_topic(relevance=None)]}
    assert AgentAction.SELECT_TOPIC in allowed_actions(undecided, BUDGET)


def test_no_usable_candidate_blocks_writing():
    """手上没有可用选题时不许往下写（工作单第三节）。

    `extract_evidence` 是"材料"变成"要发出去的东西"的第一步。一个都不合格仍
    继续抽证据、形成观点、写出正文，等于把用户不关心的材料做成了稿子——
    这一次的运行应当以"没有产出"收场，而不是硬塞。

    整条写作链都要挡住，不只是第一步：只要有一环漏掉，"不许写"就变成"晚一点写"。
    材料齐、还没有候选的运行照旧放行（用户直接指定了题目那条路径）。
    """
    materials = {"research_item_ids": ["m1"], "research_results": [object()]}
    assert AgentAction.EXTRACT_EVIDENCE in allowed_actions(materials, BUDGET)

    unrelated = {**materials, "candidate_topics": [selectable_topic(relevance=False)]}
    assert AgentAction.EXTRACT_EVIDENCE not in allowed_actions(unrelated, BUDGET)

    # 一整批都不合格（未达标 / 不相关混在一起）时，往下每一步都不可行。
    dead_batch = {
        **materials,
        "candidate_topics": [
            selectable_topic(relevance=False),
            selectable_topic(topic_id="t2").model_copy(
                update={"qualification": TopicQualification.WEAK_SUPPORT}
            ),
        ],
    }
    assert AgentAction.SELECT_TOPIC not in allowed_actions(dead_batch, BUDGET)
    assert AgentAction.EXTRACT_EVIDENCE not in allowed_actions(dead_batch, BUDGET)
    # 发现仍然可行——正确的下一步是换检索词再找一次。
    assert AgentAction.DISCOVER_TOPICS in allowed_actions(dead_batch, BUDGET)

    # 目标里已经写明了题目的运行不经过 discovery，没有候选就是没有候选。
    assert AgentAction.EXTRACT_EVIDENCE in allowed_actions(
        {**materials, "candidate_topics": []}, BUDGET
    )

    # 有可用候选但证据还没抽时，链上每一步照旧可行。
    usable = {
        **materials,
        "candidate_topics": [selectable_topic()],
        "selected_topic": "题目",
        "evidence_pack": EvidencePack(facts=[EvidenceFact(fact="事实", source_ids=["m1"])]),
    }
    assert AgentAction.GENERATE_ANGLES in allowed_actions(usable, BUDGET)


def test_topic_selection_beats_research_in_the_fallback_order():
    """回退顺序里 select_topic 排在 research_topic 前面。

    没有候选时两者都不可行，测不出差别；有可选的候选而还没选时，正确的一步是
    **先选**：直接去 research 会绕开打分排序，等于把确定性排序丢掉。
    """
    state = {"candidate_topics": [selectable_topic()]}
    assert pick_fallback(state, BUDGET) is AgentAction.SELECT_TOPIC


def test_discovery_stays_open_until_a_usable_topic_exists():
    """"发现过一次就不再发现"改成了"发现出**可用的**候选才不再发现"（工作单第三节）。

    原规则（候选列表非空即关闭）在 Run 20ab360c 里把运行卡在一个死角：候选都在，
    但一个都不该被选中。此时唯一正确的动作是**换一个检索词再发现一次**。
    重新发现仍然受研究预算约束——否则"发现不出好东西"会变成一个可以无限重试
    的动作，而那正是原规则要防的事。
    """
    assert AgentAction.DISCOVER_TOPICS in allowed_actions({}, BUDGET)

    # 候选都在，但一个都不达标/不相关 → 还可以再发现。
    dead_end = {
        "candidate_topics": [
            selectable_topic(relevance=False),
            selectable_topic(topic_id="t2").model_copy(
                update={"qualification": TopicQualification.WEAK_SUPPORT}
            ),
        ]
    }
    assert AgentAction.DISCOVER_TOPICS in allowed_actions(dead_end, BUDGET)
    # 预算用尽后不再放宽：这一步不能被无限重试。
    assert AgentAction.DISCOVER_TOPICS not in allowed_actions(
        {**dead_end, "research_count": BUDGET.max_research_calls}, BUDGET
    )

    # 有一个可用的候选 → 发现关闭（该选它，或研究它）。
    usable = {"candidate_topics": [selectable_topic(relevance=False), selectable_topic("t2")]}
    assert AgentAction.DISCOVER_TOPICS not in allowed_actions(usable, BUDGET)


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
        "candidate_topics": [selectable_topic()],
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


def test_angles_stop_being_generated_once_candidates_exist():
    """候选写出来之后角度就定了：再生成 Angle 是选不上的白工。

    `SELECT_ANGLE` 早就以这个理由被封（候选池里的正文对应的是当时那个角度），
    但 `GENERATE_ANGLES` 之前只看 `has_evidence`，于是一直可行。两者是同一件事的
    两半，必须一起关——否则 Agent 会去生成一批永远选不上的角度。
    """
    from app.agent.schemas import EvidencePack

    evidence = EvidencePack.model_validate({"facts": [{"fact": "x", "source_ids": ["s"]}]})

    # 真实需要角度时（还没有候选）照样允许
    assert AgentAction.GENERATE_ANGLES in allowed_actions(
        {"evidence_pack": evidence}, BUDGET
    )
    # 候选已经写出来之后就封口
    blocked = {"evidence_pack": evidence, "candidates": [object()]}
    assert AgentAction.GENERATE_ANGLES not in allowed_actions(blocked, BUDGET)


def test_wrapup_finishes_instead_of_repeating_angles():
    """收尾不该靠"反复生成 Angle"烧完 WRAPUP_ALLOWANCE。

    这是 RULE-013 描述的真实场景：候选池已满、最近一轮评审也已完成，但
    `generate_angles` 一直可行、又在 `FALLBACK_ORDER` 里排在 `finish` 前面，
    于是收尾一路 `generate_angles → generate_angles → …` 直到硬顶被强制 finish。

    修完之后回退的第一步就该是 `finish`：预算应当是安全边界，而不是正常完成
    任务的出口。注意这个 state 里 `EXTRACT_EVIDENCE` **仍然可行**——回退要是
    按老顺序走，选中它就是同一种"重做已完成的工作"，所以这里断的是结果不是顺序。
    """
    state = {
        "candidate_topics": [selectable_topic()],
        "selected_topic": "题目",
        "research_item_ids": ["id-1"],
        "research_count": BUDGET.max_research_calls,  # 研究预算已用尽
        "evidence_pack": _evidence_pack(),
        "angles": [object()],
        "selected_angle": object(),
        "candidates": [object(), object(), object()],  # 池子已满
        "writer_epoch": 1,
        "critic_epoch": 1,  # 最近一轮评审已完成
    }
    allowed = allowed_actions(state, BUDGET)
    assert AgentAction.FINISH in allowed
    assert AgentAction.EXTRACT_EVIDENCE in allowed  # 见证"重做"这条路确实还开着
    assert pick_fallback(state, BUDGET) is AgentAction.FINISH


def test_wrapup_still_advances_before_anything_is_written():
    """成品出现之前，回退仍然要往前走——finish 不能无条件插队。

    否则研究预算用尽的 Agent 会直接丢掉手上的材料。这条与上一条是一对：
    "优先 finish"只在**确实已经完成**时成立。
    """
    researching = {
        "candidate_topics": [selectable_topic()],
        "selected_topic": "题目",
        "research_item_ids": ["id-1"],
        "research_count": BUDGET.max_research_calls,
    }
    assert pick_fallback(researching, BUDGET) is AgentAction.EXTRACT_EVIDENCE


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
        "candidate_topics": [selectable_topic()],
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


def _evidence_pack():
    from app.agent.schemas import EvidencePack

    return EvidencePack.model_validate({"facts": [{"fact": "x", "source_ids": ["s"]}]})


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
