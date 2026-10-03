"""Agent 自主循环的端到端验证（工作单第二十二节）。

这些用例验的不是"LangGraph 里多了几个节点"，而是工作单第二十五节真正要的那件事：

> 只给一个目标，Agent 自己把剩下的做完 —— 且每一步可解释、可追踪、有预算、
> 有边界、经人工审核、不自动发布、没有破坏既有的发布状态机。

所以每条用例都盯着一个**行为**（谁决定、什么时候停、引用能不能查、改稿有没有
发生、人工审核接不接得上），而不是盯着节点数量。
"""

import re

import pytest
from langgraph.types import Command
from sqlalchemy import select

from app.agent.budget import AgentBudget
from app.agent.metrics import quality_metrics
from app.agent.nodes.finalize import make_finalize_node
from app.models import AgentRun, Draft, ResearchItem, Review
from app.agent.schemas import (
    Candidate,
    CandidateTopic,
    ClaimScope,
    EvidencePack,
    ViewpointAssessment,
    ViewpointStance,
)
from app.style.schemas import StyleRecipe
from app.repositories import create_agent_run, list_decisions, set_run_topic
from app.services.x_service import XService
from fakes import (
    FakeXApiClient,
    ScriptedLLMService,
    candidates_payload,
    critic_payload,
    decision,
    evidence_payload,
    angles_payload,
    revise_payload,
    topics_payload,
)
from support import FIXED_NOW, api_for, runtime_with_checkpointer

GOAL = "今天帮我找一个值得在中文科技 X 圈讨论的 AI 话题，并形成一条有明确观点的中文帖子。"

# 一条完整的自主路径：发现 → 选题 → 研究 → 证据 → 观点 → 选观点 → 写稿 → 评审 → 收尾。
#
# select_topic 显式带上 target=t1，而不是留给回退逻辑：回退虽然也会选到同一个，
# 但它同时会往 summary 里写一句"决策无效，已回退"——那样这条测试就分不清
# "按决策选定"和"回退兜底"哪条路走通了。
HAPPY_PATH = (
    decision("discover_topics", reason="还没有任何候选选题"),
    decision("select_topic", reason="t1 分数最高且判定达标", target="t1"),
    decision("research_topic", reason="选题定了，材料还不够"),
    decision("extract_evidence", reason="材料够了，先把事实固定下来"),
    decision("generate_angles", reason="有了事实，可以形成观点"),
    decision("select_angle", reason="观点 1 的把握最高", target="a1"),
    decision("generate_candidates", reason="观点定了，可以写稿"),
    decision("criticize", reason="写完必须自查"),
    decision("finish", reason="评审通过，交给人工"),
)


def happy_llm(**overrides) -> ScriptedLLMService:
    responses = {
        "discover": topics_payload(),
        "evidence": evidence_payload(),
        "angles": angles_payload(),
        "candidates": candidates_payload(),
        "critic": critic_payload(),
    }
    responses.update(overrides)
    return ScriptedLLMService(decisions=HAPPY_PATH, responses=responses)


def config_for(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


async def start_goal_run(session_factory, goal: str = GOAL) -> str:
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=goal)
        return str(run.id)


async def drive(runtime, session_factory, llm: ScriptedLLMService, *, budget=None, goal=GOAL):
    """跑一轮 Agent，返回 (run_id, 最终 state)。"""
    run_id = await start_goal_run(session_factory, goal)
    out = await runtime.graph.ainvoke({"run_id": run_id, "goal": goal}, config=config_for(run_id))
    return run_id, out


# --------------------------------------------------------------------------
# 主线：只给目标，一路跑到人工审核
# --------------------------------------------------------------------------


async def test_goal_only_run_stops_at_human_review(session_factory):
    llm = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)

        # 1) 停在人工审核，payload 与 V1 的形状完全一致
        assert "__interrupt__" in out
        payload = out["__interrupt__"][0].value
        assert payload["type"] == "content_review"
        assert payload["content"] == out["draft_content"]
        assert payload["draft_id"] == out["draft_id"]
        assert payload["guardrail_passed"] is True

        snapshot = await runtime.graph.aget_state(config_for(run_id))
        assert snapshot.next == ("human_review",)

        # 2) 交付的是候选池里选出的那一条，而不是"最后写的那一条"
        candidates = out["candidates"]
        assert len(candidates) == 3
        assert out["draft_content"] in [c.content for c in candidates]

        # 3) Agent 自己选定的题目被镜像到了 Run 上（运行记录列表要显示它）
        async with session_factory() as session:
            run = (await session.execute(select(AgentRun))).scalars().one()
            assert run.topic == "候选选题 1"
            assert run.goal == GOAL

        # 4) 材料落库：发现阶段 2 条 + 主题研究 2 条
        async with session_factory() as session:
            items = (await session.execute(select(ResearchItem))).scalars().all()
            assert len(items) == 4

        # 5) 只产生了一条 draft（候选池是内部工作状态，不逐条落库）
        async with session_factory() as session:
            drafts = (await session.execute(select(Draft))).scalars().all()
            assert len(drafts) == 1
            assert drafts[0].content == out["draft_content"]
            assert drafts[0].prompt_version == "5b"

        # 6) Agent 还没有权限做人工审核：review 表此时是空的
        async with session_factory() as session:
            assert (await session.execute(select(Review))).scalars().all() == []


async def test_finalize_persists_the_selected_topic(session_factory):
    """定了题目的 Run，题目必须落到 `agent_run.topic`。

    这是真机数据里一批 Run 的缺陷点（29 个产出了 draft 的 Run 全部 topic=NULL，
    而它们的 checkpoint 里 `selected_topic` **都有值**）。原因不是"没选出来"，
    而是写库那一句藏在 `if not state.get("selected_topic")` 里：`select_topic`
    只把题目写进 state，于是最正常的那条路径（选完题目直接出稿，不再经过
    research_topic）条件恒为假，库里的 topic 一直是 NULL。

    这里刻意让 state 里的 `selected_topic` 与候选分数最高的那个**不同**：
    题目以 `selected_topic` 为准（那才是 Agent 实际选的），不是按分数重挑一个。
    """
    run_id = await start_goal_run(session_factory)
    node = make_finalize_node(session_factory)

    await node(
        {
            "run_id": run_id,
            "goal": GOAL,
            "selected_topic": "Agent 实际选定的题目",
            "candidate_topics": [CandidateTopic(title="分数更高的另一个候选", trend_score=99.0)],
            "candidates": [Candidate(candidate_id="c1", label="直接观点", content="正文。")],
            "step_count": 3,
        }
    )

    async with session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalars().one()
        assert run.topic == "Agent 实际选定的题目"


async def test_finalize_falls_back_to_the_top_scoring_candidate(session_factory):
    """没有 `selected_topic` 时用**分数最高**的候选兜底。

    这条路径是真机里另一半 Run 的形态（全靠 discovery 的材料就出稿，
    从没走过 select_topic）。兜底依据必须是 trend_score——
    这一列会进运行记录列表，编一个等于让历史记录失真。
    """
    run_id = await start_goal_run(session_factory)
    node = make_finalize_node(session_factory)

    await node(
        {
            "run_id": run_id,
            "goal": GOAL,
            "candidate_topics": [
                CandidateTopic(title="低分候选", trend_score=12.0),
                CandidateTopic(title="高分候选", trend_score=88.0),
            ],
            "candidates": [Candidate(candidate_id="c1", label="直接观点", content="正文。")],
            "step_count": 3,
        }
    )

    async with session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalars().one()
        assert run.topic == "高分候选"


async def test_finalize_does_not_overwrite_a_topic_that_is_already_set(session_factory):
    """库里已经有题目时不覆盖它。

    `topic` 是运行记录列表里"这次在研究什么"的唯一说明，而它一旦写下去，
    看的人就已经照着那个在看内容了——半路改名会让历史与实际不符。
    `set_run_topic` 只在 topic 还是 NULL 时生效，这条测试钉住那个条件。
    """
    run_id = await start_goal_run(session_factory)
    async with session_factory() as session:
        await set_run_topic(session, run_id, "早就定下来的题目")

    node = make_finalize_node(session_factory)
    await node(
        {
            "run_id": run_id,
            "goal": GOAL,
            "selected_topic": "运行途中另起的题目",
            "candidate_topics": [CandidateTopic(title="又一个候选", trend_score=70.0)],
            "candidates": [Candidate(candidate_id="c1", label="直接观点", content="正文。")],
            "step_count": 3,
        }
    )

    async with session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalars().one()
        assert run.topic == "早就定下来的题目"


async def test_decision_trace_records_every_step(session_factory):
    llm = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)

        async with session_factory() as session:
            rows = await list_decisions(session, run_id)

        assert [r.action for r in rows] == [d["action"] for d in HAPPY_PATH]
        assert [r.step for r in rows] == list(range(1, len(HAPPY_PATH) + 1))

        # 每一步都要有"为什么"，执行过的步骤还要有"结果是什么"
        assert all(r.reason for r in rows)
        assert all(r.result_summary for r in rows)

        # target 被如实记下来（没有 target 的步骤为 None，不是空串）
        by_action = {r.action: r for r in rows}
        # 选题目那一步把 Planner 给的编号原样记了下来——事后要能回答
        # "当时为什么研究的是这个"，靠的就是这一列（而不是靠分数自己猜）。
        assert by_action["select_topic"].target == "t1"
        assert by_action["discover_topics"].target is None

        # 输入摘要是"看到了什么"，**不是完整 prompt**（工作单第十五节）
        summary = by_action["discover_topics"].input_summary
        assert "allowed=" in summary
        assert GOAL not in summary

        # state 里也有一条同样完整的轨迹（供运行中的 Agent 与调试使用）
        assert [e.action for e in out["decision_trace"]] == [d["action"] for d in HAPPY_PATH]


async def test_finalize_marks_the_finish_step_with_what_it_delivered(session_factory):
    """finish 不经过执行节点，它的结果由 finalize 回填——轨迹里不该有空白的一步。"""
    llm = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, _ = await drive(runtime, session_factory, llm)

        async with session_factory() as session:
            rows = await list_decisions(session, run_id)

        finish = rows[-1]
        assert finish.action == "finish"
        assert "交付候选 c" in finish.result_summary


# --------------------------------------------------------------------------
# 预算与边界
# --------------------------------------------------------------------------


async def test_infeasible_decision_is_replaced_deterministically(session_factory):
    """模型选了一个此刻不可行的动作时，换做什么由代码说了算，不是重问模型。"""
    decisions = list(HAPPY_PATH)
    # 开局就要求写稿：没有观点、没有证据，这一步无论怎么排都轮不到
    decisions[0] = decision("generate_candidates", reason="我想直接写")
    llm = happy_llm()
    llm.decisions = decisions

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)

        assert out["decision_trace"][0].action == "discover_topics"
        assert "不可行" in out["decision_trace"][0].reason

        async with session_factory() as session:
            rows = await list_decisions(session, run_id)
        assert rows[0].action == "discover_topics"
        assert "不可行" in rows[0].reason


async def test_research_budget_caps_actual_x_calls(session_factory):
    """研究预算必须真的挡住对 X 的调用——"模型说想研究"不能变成每次都去搜。"""
    client = FakeXApiClient()
    x_service = XService(client, clock=lambda: FIXED_NOW)
    calls: list[str] = []
    original = x_service.research

    # 关键字参数必须原样转下去：discover 现在会传扫描窗口
    # （start_time / end_time / observed_at），一个只认位置参数的替身会把
    # "窗口有没有真的传下去"这件事变成一个 TypeError，而不是一条断言。
    async def counting_research(query: str, max_results: int = 10, **kwargs):
        calls.append(query)
        return await original(query, max_results=max_results, **kwargs)

    x_service.research = counting_research  # type: ignore[method-assign]

    budget = AgentBudget(max_steps=4, max_research_calls=2, max_candidates=3, max_revisions=2)
    llm = happy_llm()
    # 四步全都在要求研究：第一次可行，之后就超预算了
    llm.decisions = [
        decision("discover_topics"),
        decision("research_topic", target="候选选题 1"),
        decision("research_topic", target="候选选题 2"),
        decision("research_topic", target="候选选题 3"),
    ]

    async with runtime_with_checkpointer(
        session_factory, llm_service=llm, x_service=x_service, budget=budget
    ) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)

        # 发现阶段 1 次 + 主题研究 1 次 = 预算上限
        assert len(calls) == 2
        assert out["research_count"] == 2

        # 预算用尽后**不再调用 LLM**：后续步骤是确定性的收尾
        assert llm.planner_calls() == 4

        # 而且它照样跑到了人工审核，不是"预算用完就什么都没有"
        assert "__interrupt__" in out
        assert out["draft_content"]


async def test_step_budget_stops_the_loop_and_wraps_up(session_factory):
    """步数用尽 → 停止决策（不再问模型）→ 按固定顺序收尾到出稿。"""
    budget = AgentBudget(max_steps=3, max_research_calls=4, max_candidates=3, max_revisions=2)
    llm = happy_llm()
    llm.decisions = HAPPY_PATH[:3]  # 只允许决策三步

    async with runtime_with_checkpointer(session_factory, llm_service=llm, budget=budget) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)

        assert llm.planner_calls() == 3
        assert "__interrupt__" in out
        assert out["draft_content"]

        async with session_factory() as session:
            rows = await list_decisions(session, run_id)

        assert [r.action for r in rows[:3]] == [d["action"] for d in HAPPY_PATH[:3]]
        assert any("预算已用尽" in r.reason for r in rows[3:])


async def test_agent_without_any_candidate_fails_loudly(session_factory):
    """一条候选都产不出来时，绝不能让一条空内容走到人工审核面前。"""
    budget = AgentBudget(max_steps=12, max_research_calls=0, max_candidates=3, max_revisions=2)
    llm = ScriptedLLMService(decisions=[decision("finish", reason="我不想做了")])

    async with runtime_with_checkpointer(session_factory, llm_service=llm, budget=budget) as runtime:
        run_id = await start_goal_run(session_factory)
        with pytest.raises(Exception) as excinfo:
            await runtime.graph.ainvoke(
                {"run_id": run_id, "goal": GOAL}, config=config_for(run_id)
            )

    assert "未产出任何候选正文" in str(excinfo.value)


# --------------------------------------------------------------------------
# 证据可追溯（工作单第九节）
# --------------------------------------------------------------------------


async def test_facts_with_unknown_sources_are_dropped(session_factory):
    """引用不到真实材料的"事实"必须被丢掉，而不是留在证据包里等着被引用。

    这条是本阶段最该被怀疑的一步：模型写出一个像 id 的字符串太容易了，
    而一个追溯不到出处的事实，从连不上材料的那一刻起就已经不是事实了。
    """
    fabricated = "00000000-0000-0000-0000-000000000000"
    llm = happy_llm(
        evidence=evidence_payload(
            {"fact": "编造的事实：Trump 昨天宣布了 AGI", "source_ids": [fabricated]},
            {"fact": "半真半假：引用里混了一个假出处", "source_ids": [fabricated]},
        )
    )

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)

        pack: EvidencePack = out["evidence_pack"]
        assert len(pack.facts) == 2  # 只有两条真有出处的活下来
        assert all(fabricated not in fact.source_ids for fact in pack.facts)

        # 丢弃这件事要看得到，不能静默发生
        evidence_step = next(r for r in out["decision_trace"] if r.action == "extract_evidence")
        assert "丢弃 2 条" in evidence_step.result_summary

        # 保留下来的事实，出处必须都能在真实材料里查到
        async with session_factory() as session:
            real_ids = {
                str(item.id)
                for item in (await session.execute(select(ResearchItem))).scalars().all()
            }
        for fact in pack.facts:
            assert fact.source_ids
            assert set(fact.source_ids) <= real_ids


async def test_every_angle_cites_a_real_fact(session_factory):
    """观点只能引用真实存在的事实 id（追溯链的第二跳）。"""
    llm = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

        fact_ids = {fact.fact_id for fact in out["evidence_pack"].facts}
        assert fact_ids
        for angle in out["angles"]:
            assert angle.evidence_ids
            assert set(angle.evidence_ids) <= fact_ids


# --------------------------------------------------------------------------
# 改稿循环（工作单第十二、十三节）
# --------------------------------------------------------------------------


async def test_critic_can_trigger_a_revision(session_factory):
    """Critic 说改稿 → Agent 真的去改 → 改完重新评价 → 才允许收尾。"""
    llm = happy_llm(
        critic=[critic_payload("revise", focus="c1", score=4.0), critic_payload("accept", score=9.0)],
        revise=revise_payload("改好的正文：直接给结论。"),
    )
    llm.decisions = (
        decision("discover_topics"),
        decision("research_topic", target="候选选题 1"),
        decision("extract_evidence"),
        decision("generate_angles"),
        decision("select_angle", target="a1"),
        decision("generate_candidates"),
        decision("criticize"),
        decision("revise"),
        decision("criticize"),
        decision("finish"),
    )

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)

        # 被点名的候选被原地替换：id 不变、稿次 +1、只有它变了
        revised = next(c for c in out["candidates"] if c.candidate_id == "c1")
        assert revised.revision == 1
        assert revised.content == "改好的正文：直接给结论。"
        assert all(c.revision == 0 for c in out["candidates"] if c.candidate_id != "c1")
        assert out["revision_count"] == 1

        # 交付的就是改过的那条
        assert out["draft_content"] == "改好的正文：直接给结论。"

        # 轨迹里能看到"改稿"这一步以及它改了谁
        revise_step = next(r for r in out["decision_trace"] if r.action == "revise")
        assert "改写 c1" in revise_step.result_summary


async def test_revision_budget_is_enforced(session_factory):
    """改稿次数用尽后不再改，直接交给人工——这正是工作单第十三节的要求。"""
    budget = AgentBudget(max_steps=12, max_research_calls=4, max_candidates=3, max_revisions=1)
    llm = happy_llm(
        # Critic 每一轮都要求改稿：只改一次之后就必须停下来
        critic=critic_payload("revise", focus="c1", score=4.0),
        revise=revise_payload("第一轮改写。"),
    )
    llm.decisions = (
        decision("discover_topics"),
        decision("research_topic", target="候选选题 1"),
        decision("extract_evidence"),
        decision("generate_angles"),
        decision("select_angle", target="a1"),
        decision("generate_candidates"),
        decision("criticize"),
        decision("revise"),
        decision("criticize"),
        decision("revise"),  # 超预算：会被换成别的动作
        decision("finish"),
    )

    async with runtime_with_checkpointer(session_factory, llm_service=llm, budget=budget) as runtime:
        _, out = await drive(runtime, session_factory, llm)

        assert out["revision_count"] == 1
        assert next(c for c in out["candidates"] if c.candidate_id == "c1").revision == 1
        # 第二次 revise 被拒了，轨迹里留下证据
        assert any("不可行" in entry.reason for entry in out["decision_trace"])


# --------------------------------------------------------------------------
# Phase 5A.1：改稿与重新评价的次序
# --------------------------------------------------------------------------


async def test_a_revision_cannot_be_followed_straight_away_by_another(session_factory):
    """改了稿就必须先重新评价，才能再改一次或收尾。

    这条钉的是 Phase 5A.1 在真机 smoke 里观察到的行为
    ——"critic → revise → revise → 预算耗尽"。第二次 revise 拿到的
    revision_targets 是针对**上一版**文本写的：照着旧意见改新稿，
    改稿额度花掉了，稿子却没变好。
    """
    llm = happy_llm(
        critic=[critic_payload("revise", focus="c1", score=4.0), critic_payload("accept", score=9.0)],
        revise=revise_payload("改好的正文。"),
    )
    llm.decisions = (
        decision("discover_topics"),
        decision("research_topic", target="候选选题 1"),
        decision("extract_evidence"),
        decision("generate_angles"),
        decision("select_angle", target="a1"),
        decision("generate_candidates"),
        decision("criticize"),
        decision("revise"),
        decision("revise"),  # 想连改第二次：不可行，会被换成重新评价
        decision("finish"),
    )

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    actions = [e.action for e in out["decision_trace"]]
    assert actions.count("revise") == 1
    assert out["revision_count"] == 1
    # 改稿之后紧跟的必须是重新评价，而不是又一次改稿
    assert actions[actions.index("revise") + 1] == "criticize"
    # 第二次 revise 被换成别的动作，轨迹里留下证据
    assert any("不可行" in e.reason for e in out["decision_trace"])
    assert actions[-1] == "finish"


async def test_a_revise_loop_still_terminates(session_factory):
    """Critic 每轮都要求改稿时，流程仍然会停——只是每改一次都要重新评价一次。

    预算一个都没改（max_steps=6，其余取默认），验的是终止性**不依赖**模型收敛：
    改稿额度用满后，回退顺序会把它推到别处，最后硬停。
    """
    budget = AgentBudget(max_steps=6, max_research_calls=4, max_candidates=3, max_revisions=2)
    llm = happy_llm(
        critic=critic_payload("revise", focus="c1", score=4.0),  # 永远不满意
        revise=revise_payload("又改了一版。"),
    )
    # 只给到"写出候选"为止，之后全部交给预算与回退决定
    llm.decisions = (
        decision("discover_topics"),
        decision("research_topic", target="候选选题 1"),
        decision("extract_evidence"),
        decision("generate_angles"),
        decision("select_angle", target="a1"),
        decision("generate_candidates"),
    )

    async with runtime_with_checkpointer(session_factory, llm_service=llm, budget=budget) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    actions = [e.action for e in out["decision_trace"]]
    # 改稿额度确实被用满，说明"停下来"不是因为没东西可改
    assert out["revision_count"] == budget.max_revisions
    # 每一次 revise 后面都必须有一次重新评价
    for index, action in enumerate(actions):
        if action == "revise":
            assert "criticize" in actions[index + 1 :], "改稿之后没有重新评价"
    assert actions[-1] == "finish"
    # 最终仍然走到了人工审核，而不是卡死或报错
    assert "__interrupt__" in out


async def test_quality_metrics_are_reported_for_an_agent_run(session_factory):
    """一次 Agent 运行能报出质量指标，而 topic 链路的 Run 报 None。"""
    llm = happy_llm(critic=critic_payload("accept", score=9.0))
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)
        snapshot = await runtime.graph.aget_state(config_for(run_id))

    metrics = quality_metrics(snapshot.values)
    assert metrics.total_steps == len(out["decision_trace"])
    assert metrics.candidate_count == 3
    assert metrics.critic_accept_count == 3
    assert metrics.critic_revise_count == 0
    assert metrics.critic_reject_count == 0
    assert metrics.budget_exhausted is False
    assert metrics.finish_reason  # 理由必须是原文，不能是空串
    # 非 Agent 运行没有 planner 循环，那几个计数恒为 0，报出去只会误导
    assert quality_metrics({"topic": "题"}).total_steps == 0


# --------------------------------------------------------------------------
# 与人工审核 / 发布的连接（工作单第十四节：这条链路一行都没改）
# --------------------------------------------------------------------------


async def test_agent_run_resumes_through_review_and_publish(session_factory):
    """Agent 产出的稿子走既有的审核与发布链路，发布状态机语义不变。"""
    client = FakeXApiClient()
    llm = happy_llm()
    async with runtime_with_checkpointer(
        session_factory, llm_service=llm, x_service=XService(client)
    ) as runtime:
        async with api_for(runtime) as api:
            created = await api.post("/api/runs", json={"goal": GOAL})
            assert created.status_code == 200, created.text
            body = created.json()
            run_id = body["run_id"]
            assert body["status"] == "waiting_for_review"
            agent_draft = body["draft_content"]
            assert body["review"]["content"] == agent_draft

            # 人工审核：Agent 自己不能做这一步
            reviewed = await api.post(
                f"/api/runs/{run_id}/review", json={"action": "approve"}
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["status"] == "approved"

            published = await api.post(f"/api/runs/{run_id}/publish", json={})
            assert published.status_code == 200, published.text
            assert published.json()["publish"]["status"] == "published"

    # 发出去的正是 Agent 自己写的那条正文
    assert client.created == [agent_draft]


async def test_review_can_edit_agent_content(session_factory):
    """编辑稿照旧生效：Agent 写的稿子和人写的稿子在下游是同一种东西。"""
    llm = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        async with api_for(runtime) as api:
            run_id = (await api.post("/api/runs", json={"goal": GOAL})).json()["run_id"]
            reviewed = await api.post(
                f"/api/runs/{run_id}/review",
                json={"action": "edit", "content": "人工改写后的正文"},
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["status"] == "edited"

            detail = await api.get(f"/api/runs/{run_id}")
            assert detail.json()["review"]["edited_content"] == "人工改写后的正文"


async def test_agent_run_state_is_resumable_from_checkpoint(session_factory):
    """checkpoint 里存得住 Phase 5A 的全部结构（SERDE 登记漏一个就会在这里炸）。"""
    llm = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)
        config = config_for(run_id)

        snapshot = await runtime.graph.aget_state(config)
        # 每一步的结构化产物都能从 checkpoint 里原样读回来
        assert isinstance(snapshot.values["evidence_pack"], EvidencePack)
        # 候选按 trend_score 降序落进 state，t1 就是分数最高的那个
        assert snapshot.values["candidate_topics"][0].title == "候选选题 1"
        assert snapshot.values["candidate_topics"][0].topic_id == "t1"
        assert snapshot.values["selected_angle"].angle_id == "a1"
        assert snapshot.values["critic_result"].candidate_id == "c1"
        # Phase 5B.3：`Angle` 上新增的**嵌套枚举**同样要登记进 SERDE，而漏登记的
        # 失败模式与下面 `style_recipe` 那条一模一样、同样安静：msgpack 拒绝未登记的
        # 类型、返回原始 payload，pydantic 随后在类型化字段上又把字符串校验回枚举——
        # 于是 `== "a1"` 这类断言全绿，只有日志里在响。断言 `isinstance` 才是唯一
        # 抓得住它的做法（值相等在两种形态下都成立）。
        #
        # 这里还多一层：`stance` 与 `max_stance` 之间会做**序关系**比较，而降级成
        # 字符串之后 "cautious" > "moderate" 恰好按字典序相反——那不是报错，是算错。
        angle = snapshot.values["selected_angle"]
        assert isinstance(angle.stance, ViewpointStance)
        assert isinstance(angle.claim_scope, ClaimScope)
        assert isinstance(angle.assessment, ViewpointAssessment)
        # Phase 5B.2：style_recipe 是**以顶层值**进 state 的（不是嵌在别的模型里），
        # 所以它必须在 SERDE 里显式登记。漏登记的失败模式**不报错**：msgpack 的
        # EXT 钩子拒绝它、返回原始 payload，顶层模型安静地退化成 dict——上面那些
        # 断言全部照常通过，只有恢复时下游按属性读才炸。这一条 isinstance 是唯一
        # 能抓住它的断言。
        assert isinstance(snapshot.values["style_recipe"], StyleRecipe)
        assert len(snapshot.values["decision_trace"]) == len(HAPPY_PATH)

        resumed = await runtime.graph.ainvoke(
            Command(resume={"action": "approve"}), config=config
        )
        assert resumed["human_action"] == "approve"
        assert resumed["final_content"] == out["draft_content"]


async def test_no_agent_prompt_is_sent_with_unreplaced_placeholders(session_factory):
    """发给模型的 prompt 里不得残留 `{{...}}`。

    `render()` 只替换**传进去的键**，对模板里存在、调用点没传的占位符**不报错**——
    它会把字面量 `{{STYLE}}` 原样发给模型。这不是崩溃，是安静地少了一整段输入：
    Writer 以为自己在按结构写作，其实收到的是一串花括号。Phase 5B.2 往
    candidates/revise/critic 三个模板里各插了一个 `{{STYLE}}`，正好是这类漏传的
    高发处（三个调用点、三个 kwargs，漏一个不会有任何其他测试变红）。

    这条测试断言的是**真实发出去的 prompt**，不是模板文件——它同时覆盖了
    "模板里有占位符"和"调用点补上了值"两件事，比逐个核对 kwargs 更结实。
    走一条含改稿的完整路径，把 planner 与全部工具模板都过一遍。
    """
    llm = happy_llm(
        critic=[critic_payload("revise", focus="c1", score=4.0), critic_payload("accept", score=9.0)],
        revise=revise_payload("改好的正文：直接给结论。"),
    )
    llm.decisions = (
        decision("discover_topics"),
        decision("select_topic", target="t1"),
        decision("research_topic"),
        decision("extract_evidence"),
        decision("generate_angles"),
        decision("select_angle", target="a1"),
        decision("generate_candidates"),
        decision("criticize"),
        decision("revise"),
        decision("criticize"),
        decision("finish"),
    )

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        await drive(runtime, session_factory, llm)

    kinds = {call["kind"] for call in llm.calls}
    # 先确认这条路径真的走过了我们关心的模板——否则"没有残留"可能是因为
    # 那个模板压根没被调用（改坏了调用点，测试反而变绿）。
    assert {"candidates", "critic", "revise"} <= kinds, kinds

    leftovers = [
        (call["kind"], sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", call["prompt"]))))
        for call in llm.calls
        if "{{" in call["prompt"]
    ]
    assert leftovers == []


async def test_goal_and_topic_are_mutually_exclusive(session_factory):
    """两个都给、或都不给，都是有歧义的输入，拒收（422）。"""
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            both = await api.post("/api/runs", json={"topic": "x", "goal": "y"})
            neither = await api.post("/api/runs", json={})
            assert both.status_code == 422
            assert neither.status_code == 422
