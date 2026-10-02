"""工具层的判定逻辑（不需要数据库）。

这些函数都在做同一件事：**模型说了什么** → **实际按什么执行**。
它们是 Agent 里最容易被"看起来正常"蒙混过去的地方——一个引错的 id、
一次没生效的回退，都不会报错，只会让结果悄悄偏离。所以逐个钉死。
"""

import json
from types import SimpleNamespace

from app.agent import search_query
from app.agent.nodes.finalize import pick_final_candidate
from app.agent.persona import load_persona
from app.agent.schemas import (
    Angle,
    Candidate,
    CandidateTopic,
    ClaimScope,
    CriticResult,
    EvidenceFact,
    EvidencePack,
)
from app.agent.tools.angles import make_angles_tool, make_select_angle_tool
from app.agent.tools.critic import _bind_to_candidates, _choose_focus, make_critic_tool
from app.agent.tools.evidence import _keep_traceable
from app.agent.tools.research import _choose_topic, _deepen_options
from app.agent.tools.revise import _pick_target, make_revise_tool
from app.agent.schemas import AgentDecision, CriticReport
from fakes import ScriptedLLMService, critic_payload, revise_payload

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


def topics(count: int = 3) -> list[CandidateTopic]:
    """候选选题，分数递减——"取分数最高的那个"于是有唯一正确答案。"""
    return [
        CandidateTopic(
            topic_id=f"t{i}",
            title=f"选题 {i}",
            trend_score=round(90.0 - i * 10, 2),
            author_count=2,
            discussion_count=2,
        )
        for i in range(1, count + 1)
    ]


def test_planner_target_is_honoured_only_when_it_is_a_real_candidate():
    state = {"candidate_topics": topics(), "goal": "目标"}
    chosen, how = _choose_topic(state, AgentDecision(action="research_topic", reason="x", target="选题 2"))
    assert chosen == "选题 2"
    assert "按决策" in how


def test_topic_target_also_accepts_the_topic_id():
    """target 认编号也认标题：Planner 在 prompt 里两个都看得到，
    只认一种的话，另一种写法会被当成"无效决策"而触发一次莫名其妙的回退。"""
    chosen, how = _choose_topic(
        {"candidate_topics": topics(), "goal": "目标"},
        AgentDecision(action="research_topic", reason="x", target="t2"),
    )
    assert chosen == "选题 2"
    assert "按决策" in how


def test_invalid_topic_falls_back_to_the_highest_scoring_and_says_so():
    state = {"candidate_topics": topics(), "goal": "目标"}
    chosen, how = _choose_topic(
        state, AgentDecision(action="research_topic", reason="x", target="模型改了措辞的题目")
    )
    assert chosen == "选题 1"
    assert "回退" in how


def test_an_already_selected_topic_wins_over_the_decision():
    """已经选定过就不再重挑。

    `select_topic` 的结果是确定性排序算出来的，而 Planner 的 target 只是措辞——
    让措辞盖过排序，等于把"选哪个"这件事又交回给模型。
    """
    state = {"candidate_topics": topics(), "selected_topic": "已经定下的题", "goal": "目标"}
    chosen, how = _choose_topic(
        state, AgentDecision(action="research_topic", reason="x", target="选题 2")
    )
    assert chosen == "已经定下的题"
    assert "沿用" in how


def test_topic_falls_back_to_the_goal_when_nothing_was_discovered():
    chosen, how = _choose_topic({}, AgentDecision(action="research_topic", reason="x"))
    assert chosen == ""  # 没有 goal 也没有候选
    assert "兜底" in how


def _deepen_pick(state, decision):
    """深化研究的检索词：候选顺序由 `_deepen_options` 给，取第一个**能用**的。

    生产里这两步是一起调的（`make_deepen_tool`），测试里也一起调——
    只测候选顺序，会让"顺序里混进一个 X 用不了的词"这类问题整片漏过去。
    """
    return search_query.choose(_deepen_options(state, decision))


def test_deepen_prefers_the_explicit_query_then_the_known_gaps():
    pack = EvidencePack(unknowns=["还不知道价格"], counterpoints=["有人说没用"])
    state = {"selected_topic": "原题", "evidence_pack": pack}

    explicit, how = _deepen_pick(
        state,
        AgentDecision(action="deepen_research", reason="x", arguments={"query": "换个词搜"}),
    )
    assert (explicit, "指定" in how) == ("换个词搜", True)

    gap, how = _deepen_pick(state, AgentDecision(action="deepen_research", reason="x"))
    assert gap == "还不知道价格"
    assert "未知" in how


def test_deepen_uses_the_topic_as_the_last_resort():
    query, how = _deepen_pick(
        {"selected_topic": "原题"}, AgentDecision(action="deepen_research", reason="x")
    )
    assert query == "原题"
    assert "原题" in how


def test_deepen_skips_a_query_x_cannot_parse():
    """优先级里的每一条都可能含 X 不接受的字符，所以要取第一个**能用**的。

    Planner 给的检索词是自由文本，未知项更常常是一句话（`每天/每周` 这样的
    写法很常见）。跳过它们、用下一条，比拿一个必然 400 的 query 去撞好。
    """
    pack = EvidencePack(unknowns=["还不知道价格"], counterpoints=["有人说没用"])
    state = {"selected_topic": "原题", "evidence_pack": pack}

    query, how = _deepen_pick(
        state,
        AgentDecision(
            action="deepen_research", reason="x", arguments={"query": "https://t.co/abc"}
        ),
    )
    assert query == "还不知道价格"
    assert "未知" in how


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


# --------------------------------------------------------------------------
# Phase 5A.1 第二阶段：改稿之后旧评价必须作废
# --------------------------------------------------------------------------


def revised_pool() -> list[Candidate]:
    """c1 已经改过一稿（revision=1），c2/c3 还是初稿。"""
    return [
        Candidate(candidate_id="c1", label="直接观点", content="改过的正文", revision=1),
        Candidate(candidate_id="c2", label="信息密集", content="正文2"),
        Candidate(candidate_id="c3", label="反共识", content="正文3"),
    ]


def test_a_revised_candidate_is_not_backed_by_the_score_it_had_before():
    """改稿作废旧评价之后，被改的那条不能再凭旧分数胜出。

    这是本阶段的核心断言。候选是**原地替换**的——candidate_id 不变、只换正文，
    而 `finalize` 正是按 candidate_id 取分数的。所以只要旧评价还留在
    `critic_results` 里，一条改坏了的稿子仍然能顶着改稿**前**的高分被交付。
    """
    candidates = revised_pool()
    stale = verdict("c1", "revise", 9.9)  # 针对"旧正文"打出来的高分
    fresh = verdict("c2", "accept", 2.0)

    # 旧评价还在时：c1 凭旧分数压过真正被评价过的 c2（＝要消灭的行为）
    with_stale = {"candidates": candidates, "critic_results": [stale, fresh]}
    assert pick_final_candidate(with_stale).candidate_id == "c1"

    # revise 工具摘掉旧评价之后：c1 落回"未被评价"一档，不再是已评价候选，
    # 于是 c2 胜出——c2 的分数才是唯一有依据的那个
    without_stale = {"candidates": candidates, "critic_results": [fresh]}
    assert pick_final_candidate(without_stale).candidate_id == "c2"


def test_a_rejected_candidate_never_outranks_a_reviewed_one():
    """被否的候选即使分最高也垫底（工作单第十一节：reject 不该被分数翻盘）。"""
    state = {
        "candidates": pool(),
        "critic_results": [verdict("c3", "reject", 10.0), verdict("c2", "accept", 2.0)],
    }
    assert pick_final_candidate(state).candidate_id == "c2"


async def test_revise_invalidates_the_verdict_it_just_revised():
    """revise 工具本身的两件事：摘掉旧评价、清空聚焦对象。

    缺第二件会怎样：`revision_pending` 只看 `critic_result`，不清空的话 revise
    仍然可行、又排在 FALLBACK_ORDER 首位，于是能连着改两次——而第二次拿到的
    revision_targets 是针对**上一版**文本写的，等于照着旧意见改新稿。
    """
    candidates = pool()
    state = {
        "run_id": "run-1",
        "goal": "目标",
        "candidates": candidates,
        "critic_result": verdict("c1", "revise", 4.0),
        "critic_results": [verdict("c1", "revise", 4.0), verdict("c2", "accept", 8.0)],
        "evidence_pack": EvidencePack(facts=[EvidenceFact(fact="事实", source_ids=["s"])]),
        "writer_epoch": 1,
        "critic_epoch": 1,
    }
    llm = ScriptedLLMService(responses={"revise": revise_payload("改好的正文。")})
    tool = make_revise_tool(llm, load_persona())

    delta = (await tool(state, AgentDecision(action="revise", reason="改", target="c1"))).delta

    # 旧评价被摘掉，别人的不受影响
    assert [r.candidate_id for r in delta["critic_results"]] == ["c2"]
    assert delta["critic_result"] is None
    # 原地替换：id 不变、稿次 +1
    revised = next(c for c in delta["candidates"] if c.candidate_id == "c1")
    assert (revised.revision, revised.content) == (1, "改好的正文。")
    assert [c.candidate_id for c in delta["candidates"]] == ["c1", "c2", "c3"]
    # 改完必须重新评价的门槛照旧由 epoch 保证
    assert delta["writer_epoch"] == 2


async def test_critic_tally_accumulates_across_rounds():
    """tally 必须**累计**：critic_results 每轮被整体覆盖，事后反推不出来。

    改回"从 critic_results 现算"会让指标永远只反映最后一轮，
    而"这一轮 run 里 Critic 一共 accept 了几条"正是要看的东西。
    """
    state = {
        "run_id": "run-1",
        "goal": "目标",
        "candidates": pool(),
        "writer_epoch": 1,
        "critic_epoch": 0,
        "evidence_pack": EvidencePack(facts=[EvidenceFact(fact="事实", source_ids=["s"])]),
    }
    llm = ScriptedLLMService(
        responses={"critic": [critic_payload("revise", focus="c1"), critic_payload("accept", score=9.0)]}
    )
    tool = make_critic_tool(llm, load_persona())

    first = (await tool(state, AgentDecision(action="criticize", reason="评"))).delta
    assert first["critic_tally"] == {"revise": 3}  # 三条候选各判一次 revise

    second = (await tool({**state, **first, "writer_epoch": 2}, AgentDecision(action="criticize", reason="重评"))).delta
    assert second["critic_tally"] == {"revise": 3, "accept": 3}  # 上一轮的累计没有丢
    # 但 critic_results 只剩最后一轮——这正是不用它的原因
    assert len(second["critic_results"]) == 3
    assert {r.decision for r in second["critic_results"]} == {"accept"}


# --------------------------------------------------------------------------
# Phase 5B.3：观点由工具赋分、由工具编号
# --------------------------------------------------------------------------

FACT_ONE = "OpenAI 把 API 价格下调了四成"
FACT_TWO = "Anthropic 同时下调了批量推理价格"
FACT_THREE = "两家在同一周内调整了企业版定价"


def evidence_state(**extra) -> dict:
    """一份"三条事实、三条来源"的 state，够算出有区分度的观感分数。

    `research_item_ids` 与 `research_results` 一一对应（`zip` 是打分层的
    既定不变量），作者名用假的字符串即可——打分只读 `author_username`。
    """
    state = {
        "run_id": "run-1",
        "goal": "目标",
        "selected_topic": "选题",
        "evidence_pack": EvidencePack(
            facts=[
                EvidenceFact(fact_id="f1", fact=FACT_ONE, source_ids=["s1"]),
                EvidenceFact(fact_id="f2", fact=FACT_TWO, source_ids=["s2"]),
                EvidenceFact(fact_id="f3", fact=FACT_THREE, source_ids=["s3"]),
            ],
            consensus=["厂商在争夺企业客户"],
            counterpoints=["降价可能只是季度促销"],
            unknowns=["是否长期"],
            source_ids=["s1", "s2", "s3"],
        ),
        "research_item_ids": ["s1", "s2", "s3"],
        "research_results": [
            SimpleNamespace(author_username="alice", content="正文一"),
            SimpleNamespace(author_username="bob", content="正文二"),
            SimpleNamespace(author_username="carol", content="正文三"),
        ],
    }
    state.update(extra)
    return state


def angles_llm(angles, **extra) -> ScriptedLLMService:
    """一个只回答 angles 的假 LLM。"""
    return ScriptedLLMService(responses={"angles": json.dumps({"angles": angles}, ensure_ascii=False), **extra})


def draft_json(**overrides) -> dict:
    draft = {
        "claim": "定价权正在从厂商转向买方",
        "reasoning": "两家在同一周做了同一件事",
        "evidence_ids": ["f1", "f2", "f3"],
        "counterpoints": ["也许只是季度促销"],
        "counterpoint_evidence_ids": ["f1"],
        "claim_scope": "segment",
    }
    draft.update(overrides)
    return draft


async def test_generate_angles_assigns_scores_and_ids_itself():
    """分数字段一个都不采信模型；id 在确定性排序**之后**才赋。

    名次对不上分数的编号比没有编号更误导：`format_state` 里 id 与分数会同屏
    出现，a1 必须是分最高的那个。
    """
    llm = angles_llm(
        [
            draft_json(claim="弱判断", evidence_ids=["f1"]),
            draft_json(claim="强判断：定价权正在转向买方"),
        ]
    )
    delta = (
        await make_angles_tool(llm)(evidence_state(), AgentDecision(action="generate_angles", reason="x"))
    ).delta

    angles = delta["angles"]
    assert [a.angle_id for a in angles] == ["a1", "a2"]
    assert angles[0].claim == "强判断：定价权正在转向买方"
    assert angles[0].viewpoint_score > angles[1].viewpoint_score
    assert angles[0].confidence > angles[1].confidence
    # 分数是 Python 算的，草稿里根本没有这一项——模型给不出它
    assert angles[0].assessment is not None
    assert angles[0].assessment.bound_facts == 3
    assert angles[0].viewpoint_score == round(
        100.0 * (
            0.30 * angles[0].assessment.evidence_support
            + 0.15 * angles[0].assessment.source_diversity
            + 0.20 * angles[0].assessment.specificity
            + 0.20 * angles[0].assessment.newness_contribution
            + 0.15 * angles[0].assessment.counterpoint_awareness
        ),
        2,
    )


async def test_generate_angles_carries_the_draft_text_through():
    """推理层、反方、假设、影响、申报——这些是文本，原样搬进 Angle。

    漏掉任何一项都不会报错，只会让 Writer 拿到一个"只有一句话"的观点。
    """
    llm = angles_llm([draft_json(restates_known_material=True, novelty_note="换了个角度")])
    delta = (
        await make_angles_tool(llm)(evidence_state(), AgentDecision(action="generate_angles", reason="x"))
    ).delta

    angle = delta["angles"][0]
    assert angle.reasoning == "两家在同一周做了同一件事"
    assert angle.counterpoints == ["也许只是季度促销"]
    assert angle.counterpoint_evidence_ids == ["f1"]
    assert angle.claim_scope is ClaimScope.SEGMENT
    assert angle.novelty_note == "换了个角度"
    # 自述"只是复述"必须一路带到 Angle 上：`select_angle` 会用同一份打分函数
    # 复算，丢了这项会让复算出的 novelty 比生成时高，state 里就出现两份不一致的分。
    assert angle.restates_known_material is True
    assert angle.assessment.novelty == 0.0


async def test_angles_without_reachable_evidence_are_dropped_not_kept_as_opinions():
    """引不到 fact 的草稿整条丢弃。

    留着一个"没有依据但说得漂亮"的观点参与排序，就是给它一次拿高分的机会。
    """
    llm = angles_llm(
        [
            draft_json(claim="有依据的判断"),
            draft_json(claim="没有依据的判断", evidence_ids=["f99"]),
            draft_json(claim="空引用的判断", evidence_ids=[]),
        ]
    )
    result = await make_angles_tool(llm)(
        evidence_state(), AgentDecision(action="generate_angles", reason="x")
    )

    assert [a.claim for a in result.delta["angles"]] == ["有依据的判断"]
    # 丢了几条要写进 summary：静默丢弃与"模型只写了一条"看起来一模一样
    assert "丢弃 2 个" in result.summary


async def test_counterpoint_ids_that_cannot_be_verified_are_dropped_but_the_sentence_stays():
    """反方引不到真实事实时只丢**引用**，那句反驳留着。

    观点仍然"提到过"这个反方，只是它不可核验，因此拿不到绑定分——
    按绑定分算，正是工作单第八节要的"找过反方"与"只是写了一句"的区别。
    """
    llm = angles_llm(
        [draft_json(counterpoint_evidence_ids=["f1", "f99", "f98"])]
    )
    delta = (
        await make_angles_tool(llm)(evidence_state(), AgentDecision(action="generate_angles", reason="x"))
    ).delta

    angle = delta["angles"][0]
    assert angle.counterpoint_evidence_ids == ["f1"]
    assert angle.counterpoints == ["也许只是季度促销"]
    assert angle.assessment.declared_counterpoints == 1
    assert angle.assessment.bound_counterpoints == 1


async def test_regenerating_angles_clears_the_previous_selection():
    """重生成必须把 `selected_angle` 一起清掉。

    否则它继续指向一个**不在新 `angles` 里**的对象，而 Writer 照着它写稿：
    没有报错，只是这一轮的观点与正文对不上。Phase 5A 起这条路径不可达
    （budget 在有候选之后挡住 generate_angles），但研究回路会把它变成可达。
    """
    llm = angles_llm([draft_json(), draft_json(claim="另一个判断", evidence_ids=["f2"])])
    stale = Angle(angle_id="a9", claim="上一轮选的观点")
    state = evidence_state(angles=[stale], selected_angle=stale)

    delta = (
        await make_angles_tool(llm)(state, AgentDecision(action="generate_angles", reason="x"))
    ).delta

    assert delta["selected_angle"] is None
    assert {a.angle_id for a in delta["angles"]} == {"a1", "a2"}


# --- select_angle ----------------------------------------------------------


async def select(state, target: str = ""):
    """跑一次 select_angle。"""
    return await make_select_angle_tool()(
        state, AgentDecision(action="select_angle", reason="x", target=target)
    )


async def test_select_angle_takes_the_top_scorer_by_id_or_by_claim():
    llm = angles_llm(
        [
            draft_json(claim="弱判断", evidence_ids=["f1"]),
            draft_json(claim="强判断：定价权正在转向买方"),
        ]
    )
    state = evidence_state()
    state.update(
        (
            await make_angles_tool(llm)(state, AgentDecision(action="generate_angles", reason="x"))
        ).delta
    )

    by_id = (await select(state, "a1")).delta["selected_angle"]
    assert by_id.angle_id == "a1"
    assert by_id.claim == "强判断：定价权正在转向买方"

    # claim 原文也认——Planner 在 prompt 里两个都看得到
    by_claim = (await select(state, "强判断：定价权正在转向买方")).delta["selected_angle"]
    assert by_claim.angle_id == "a1"


async def test_select_angle_ignores_a_target_that_loses_by_more_than_the_margin():
    """工作单第二十节：排序已经明确时不让模型推翻，但要把差了多少分说出来。"""
    llm = angles_llm(
        [
            draft_json(claim="弱判断", evidence_ids=["f1"]),
            draft_json(claim="强判断：定价权正在转向买方"),
        ]
    )
    state = evidence_state()
    state.update(
        (
            await make_angles_tool(llm)(state, AgentDecision(action="generate_angles", reason="x"))
        ).delta
    )

    result = await select(state, "弱判断")
    assert result.delta["selected_angle"].angle_id == "a1"
    assert "超过容差" in result.summary


async def test_select_angle_honours_a_target_inside_the_tie_band():
    """并列区间里"哪个第一"本来就没分出高下，此时决策说了算。"""
    llm = angles_llm(
        [
            draft_json(claim="判断甲", evidence_ids=["f1", "f2"]),
            draft_json(claim="判断乙", evidence_ids=["f1", "f2"]),
        ]
    )
    state = evidence_state()
    state.update(
        (
            await make_angles_tool(llm)(state, AgentDecision(action="generate_angles", reason="x"))
        ).delta
    )

    result = await select(state, "判断甲")
    assert result.delta["selected_angle"].claim == "判断甲"
    assert "容差" in result.summary


async def test_select_angle_recomputes_instead_of_trusting_stored_numbers():
    """存下来的分数只是给报告看的；**排序的依据必须是当下这份输入算出来的**。

    老 checkpoint 里的 Angle 没有 assessment，`confidence` 还可能是模型当时
    自己给的数。相信它，等于让一份无法复算的历史数字决定今天选哪个观点。
    """
    state = evidence_state(
        angles=[
            Angle(angle_id="a1", claim="被高估的判断", confidence=0.99, evidence_ids=["f1"]),
            Angle(angle_id="a2", claim="被低估的判断", confidence=0.01, evidence_ids=["f1", "f2", "f3"]),
        ]
    )
    chosen = (await select(state, "")).delta["selected_angle"]
    assert chosen.angle_id == "a2"
    # 写回的是重算值，不是那座 0.99 的旧牌坊
    assert chosen.confidence < 0.99
    assert chosen.viewpoint_score is not None


async def test_select_angle_with_no_angles_is_a_no_op():
    result = await select(evidence_state(angles=[]), "a1")
    assert result.delta == {}

