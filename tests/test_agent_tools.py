"""工具层的判定逻辑（不需要数据库）。

这些函数都在做同一件事：**模型说了什么** → **实际按什么执行**。
它们是 Agent 里最容易被"看起来正常"蒙混过去的地方——一个引错的 id、
一次没生效的回退，都不会报错，只会让结果悄悄偏离。所以逐个钉死。
"""

from app.agent.nodes.finalize import pick_final_candidate
from app.agent.schemas import (
    Angle,
    Candidate,
    CriticResult,
    EvidenceFact,
    EvidencePack,
    TopicCandidate,
)
from app.agent.tools.critic import _bind_to_candidates, _choose_focus
from app.agent.tools.evidence import _keep_traceable
from app.agent.tools.research import _choose_topic, _deepen_query
from app.agent.tools.revise import _pick_target
from app.agent.schemas import AgentDecision, CriticReport

REAL_IDS = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]


def verdict(candidate_id: str, decision: str = "accept", score: float = 8.0) -> CriticResult:
    return CriticResult(candidate_id=candidate_id, decision=decision, score=score)


# --------------------------------------------------------------------------
# Critic 的评价如何对到候选上
# --------------------------------------------------------------------------


def test_verdicts_are_bound_by_id():
    report = CriticReport(results=[verdict("c2"), verdict("c1")], focus_candidate_id="c1")
    bound = _bind_to_candidates(report, ["c1", "c2", "c3"])
    assert [r.candidate_id for r in bound] == ["c2", "c1"]


def test_duplicate_verdicts_are_collapsed():
    """模型对同一条候选评两次时，"评价了 N 条"这个数字不能虚高。"""
    report = CriticReport(
        results=[verdict("c1"), verdict("c1", score=3.0), verdict("c2")],
        focus_candidate_id="c1",
    )
    bound = _bind_to_candidates(report, ["c1", "c2"])
    assert [r.candidate_id for r in bound] == ["c1", "c2"]
    assert bound[0].score == 8.0  # 保留第一次出现的那条


def test_wrong_ids_fall_back_to_position():
    """全部对不上时按位置兜底：一条抄错 id 的评价，好过没有评价。"""
    report = CriticReport(
        results=[verdict("candidate_1"), verdict("candidate_2")], focus_candidate_id="candidate_1"
    )
    bound = _bind_to_candidates(report, ["c1", "c2"])
    assert [r.candidate_id for r in bound] == ["c1", "c2"]


def test_partial_matches_do_not_get_positionally_rebound():
    """一半对得上时不能按位置兜底：那会把已经对上的那条覆盖成别人的评价。"""
    report = CriticReport(results=[verdict("c2"), verdict("乱写的")], focus_candidate_id="")
    bound = _bind_to_candidates(report, ["c1", "c2", "c3"])
    assert [r.candidate_id for r in bound] == ["c2"]


def test_focus_falls_back_to_the_verdict_that_asked_for_a_revision():
    """模型漏填 focus 时，必须仍然聚焦到"要求改稿"的那条。

    否则：Critic 说 c2 要改，流程却聚焦到评分最高的 c1（accept），
      revise 永远不触发，整个改稿循环**静默失效**——而且不会有任何报错。
    """
    results = [verdict("c1", "accept", 9.0), verdict("c2", "revise", 4.0)]
    assert _choose_focus(results, "").candidate_id == "c2"
    assert _choose_focus(results, "不存在的 id").candidate_id == "c2"
    # 指名了就用指名的
    assert _choose_focus(results, "c1").candidate_id == "c1"


def test_focus_defaults_to_the_best_scoring_candidate():
    results = [verdict("c1", "accept", 5.0), verdict("c2", "accept", 9.0)]
    assert _choose_focus(results, "").candidate_id == "c2"


# --------------------------------------------------------------------------
# 证据可追溯
# --------------------------------------------------------------------------


def test_facts_are_renumbered_and_unreachable_ones_dropped():
    pack = EvidencePack.model_validate(
        {
            "facts": [
                {"fact_id": "模型自己编的", "fact": "A", "source_ids": [REAL_IDS[0]]},
                {"fact": "B", "source_ids": ["假的"]},
                {"fact": "C", "source_ids": [REAL_IDS[1], "假的"]},
            ]
        }
    )
    clean, dropped = _keep_traceable(pack, set(REAL_IDS))

    assert dropped == 1
    # fact_id 由工具重新编号，不采信模型给的值（Angle 要按 id 引用它们）
    assert [f.fact_id for f in clean.facts] == ["f1", "f2"]
    assert clean.facts[1].source_ids == [REAL_IDS[1]]  # 混进来的假出处被剔掉
    assert clean.source_ids == sorted(REAL_IDS)


def test_facts_without_any_source_are_never_kept():
    pack = EvidencePack(facts=[EvidenceFact(fact="无出处的事实", source_ids=[])])
    clean, dropped = _keep_traceable(pack, set(REAL_IDS))
    assert clean.facts == []
    assert dropped == 1


# --------------------------------------------------------------------------
# 选题与检索词的回退
# --------------------------------------------------------------------------


def topics(count: int = 3) -> list[TopicCandidate]:
    return [
        TopicCandidate(topic=f"选题 {i}", confidence=round(0.9 - i * 0.1, 2))
        for i in range(1, count + 1)
    ]


def test_planner_target_is_honoured_only_when_it_is_a_real_candidate():
    state = {"candidate_topics": topics(), "goal": "目标"}
    chosen, how = _choose_topic(state, AgentDecision(action="research_topic", reason="x", target="选题 2"))
    assert chosen == "选题 2"
    assert "按决策" in how


def test_invalid_topic_falls_back_to_the_most_confident_and_says_so():
    state = {"candidate_topics": topics(), "goal": "目标"}
    chosen, how = _choose_topic(
        state, AgentDecision(action="research_topic", reason="x", target="模型改了措辞的题目")
    )
    assert chosen == "选题 1"
    assert "回退" in how


def test_topic_falls_back_to_the_goal_when_nothing_was_discovered():
    chosen, how = _choose_topic({}, AgentDecision(action="research_topic", reason="x"))
    assert chosen == ""  # 没有 goal 也没有候选
    assert "兜底" in how


def test_deepen_prefers_the_explicit_query_then_the_known_gaps():
    pack = EvidencePack(unknowns=["还不知道价格"], counterpoints=["有人说没用"])
    state = {"selected_topic": "原题", "evidence_pack": pack}

    explicit, how = _deepen_query(
        state,
        AgentDecision(action="deepen_research", reason="x", arguments={"query": "换个词搜"}),
    )
    assert (explicit, "指定" in how) == ("换个词搜", True)

    gap, how = _deepen_query(state, AgentDecision(action="deepen_research", reason="x"))
    assert gap == "还不知道价格"
    assert "未知" in how


def test_deepen_uses_the_topic_as_the_last_resort():
    query, how = _deepen_query(
        {"selected_topic": "原题"}, AgentDecision(action="deepen_research", reason="x")
    )
    assert query == "原题"
    assert "原题" in how


# --------------------------------------------------------------------------
# 改哪一条 / 交付哪一条
# --------------------------------------------------------------------------


def pool() -> list[Candidate]:
    return [Candidate(candidate_id=f"c{i}", label="直接观点", content=f"正文{i}") for i in (1, 2, 3)]


def test_revise_targets_the_critic_verdict_by_default():
    candidates = pool()
    target = _pick_target(candidates, verdict("c2", "revise"), None)
    assert target.candidate_id == "c2"


def test_revise_honours_an_explicit_target_but_never_a_bogus_one():
    candidates = pool()
    assert _pick_target(candidates, verdict("c2", "revise"), "c3").candidate_id == "c3"
    # 指到一个不存在的候选时回到 Critic 指的那条，而不是改到别的一条上
    assert _pick_target(candidates, verdict("c2", "revise"), "c9").candidate_id == "c2"


def test_final_candidate_prefers_the_best_scoring_accepted_one():
    candidates = pool()
    state = {
        "candidates": candidates,
        "critic_results": [verdict("c1", "accept", 6.0), verdict("c2", "accept", 9.0)],
    }
    assert pick_final_candidate(state).candidate_id == "c2"


def test_final_candidate_ranks_unevaluated_above_rejected():
    candidates = pool()
    state = {
        "candidates": candidates,
        "critic_results": [verdict("c1", "reject", 9.0), verdict("c2", "accept", 1.0)],
    }
    # c2 分低但没被否，仍然优于被否的 c1；c3 没被评价，排在两者之后
    assert pick_final_candidate(state).candidate_id == "c2"

    only_rejected = {
        "candidates": candidates[:1],
        "critic_results": [verdict("c1", "reject", 2.0)],
    }
    # 只有被否的候选时照样交付（否决记录留在 critic_results 里让人看见），
    # 而不是让整个 Run 失败
    assert pick_final_candidate(only_rejected).candidate_id == "c1"


def test_final_candidate_without_any_verdict_takes_the_first():
    assert pick_final_candidate({"candidates": pool()}).candidate_id == "c1"


def test_angles_can_only_cite_facts():
    """Angle 不能直接引原始材料：绕过证据等于让 EvidencePack 变成摆设。"""
    angle = Angle(claim="观点")
    assert "source_ids" not in Angle.model_fields
    assert "evidence_ids" in Angle.model_fields
    assert angle.evidence_ids == []
