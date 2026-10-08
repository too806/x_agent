"""自动审阅者的判据：**每条判据各一个反例**。

这是整个无人值守自动发布里唯一"替人做决定"的地方，所以这里的测试不是覆盖率，
是一张"哪条路能让一条烂帖发出去"的清单。每条判据都有一个专门的反例，
而且每条反例只触发那**一条**——否则一条测试红了说不清是哪条判据漏的。

分三段：

1. `resolve_delivered_verdict` —— 最容易写错的那一处（交付候选 vs
   `state["critic_result"]`）。
2. `evaluate_auto_publish` —— 六条判据，每条一个反例，外加一条"提示性检查
   不该拦人"的正例（`too_long`）。
3. 一致率与门槛 —— 影子转正式的依据，包括 `None ≠ 0`。
"""

from dataclasses import replace

from app.agent.schemas import Candidate, CriticResult
from app.models.auto_review import MODE_SHADOW, VERDICT_APPROVE, VERDICT_REJECT
from app.services.auto_review import (
    AgreementSummary,
    evaluate_auto_publish,
    evaluate_go_live_gate,
    resolve_delivered_verdict,
    summarize_agreement,
)
from app.services.automation_config import AutomationConfig

#: 一条绝对干净、绝对不重复的正文（对所有判据都中性）。
CLEAN = "便宜的推理正在把注意力变成稀缺资源，这件事比模型能力本身更值得盯。"


def config(**overrides) -> AutomationConfig:
    base = dict(
        enabled=True,
        mode=MODE_SHADOW,
        goals=["写一条帖子"],
        min_critic_score=7.0,
        max_publishes_per_day=1,
    )
    base.update(overrides)
    return AutomationConfig(**base)


def candidate(cid: str, content: str = CLEAN, revision: int = 0) -> Candidate:
    return Candidate(candidate_id=cid, label="直接观点", content=content, revision=revision)


def verdict(cid: str, decision: str = "accept", score: float = 8.5) -> CriticResult:
    return CriticResult(candidate_id=cid, decision=decision, score=score)


def state(*, candidates, critic_results=(), critic_result=None, topic=None):
    """一份**只含判据会用到的字段**的 state。

    刻意不塞 `run_id` 之类：这个函数的形状就是"判据读了哪些东西"的清单，
    多一个用不上的字段会让"判据到底依赖什么"变得看不出来。
    """
    return {
        "candidates": list(candidates),
        "critic_results": list(critic_results),
        "critic_result": critic_result,
        "selected_topic": topic,
    }


def evaluate(st, *, content: str = CLEAN, **kwargs):
    kwargs.setdefault("config", config())
    return evaluate_auto_publish(content=content, state=st, **kwargs)


# ---------------------------------------------------------------------------
# 1. 交付候选 vs state["critic_result"]
# ---------------------------------------------------------------------------


def test_verdict_comes_from_the_delivered_candidate_not_the_focused_one():
    """**这是本模块最重要的一条回归。**

    `state["critic_result"]` 是"该关注的那条"，`finalize` 交付的是
    `pick_final_candidate` 选出来的**另一条**。这里构造的正是这种不一致：
    critic_result 指向一条分数不够的候选，而真正会交付的那条分数是够的。

    读错了来源的实现会**拒绝**；读对了会放行。方向不重要，重要的是
    两者必须给出不同的结论——否则这条测试没有在测任何东西。
    """
    delivered = candidate("c1")
    other = candidate("c2")
    st = state(
        candidates=[delivered, other],
        critic_results=[verdict("c1", "accept", 9.0), verdict("c2", "accept", 6.0)],
        # 陈旧/错位的"关注点"：分数低于门槛
        critic_result=verdict("c2", "accept", 6.0),
    )

    decision = evaluate(st)

    assert decision.checks["critic"]["candidate_id"] == "c1"
    assert decision.critic_score == 9.0
    assert decision.approved is True

    # 反证：若判据读的是 critic_result，这条会因为 6.0 < 7.0 被拒。
    assert st["critic_result"].score < config().min_critic_score


def test_stale_focused_verdict_cannot_approve_an_unevaluated_candidate():
    """改稿之后没重新评价时，`critic_result` 会指向一个**已经不在候选池里**的 id。

    这正是"直接读 critic_result 会放行一条 Critic 从没说过 accept 的稿"的真实形态：
    c1 被改成了 c2，评审记录还停在 c1 上，而 c2 从没被看过。
    正确行为是**拒绝**（没有依据替人放行）。
    """
    st = state(
        candidates=[candidate("c2", revision=1)],
        critic_results=[verdict("c1", "accept", 9.5)],
        critic_result=verdict("c1", "accept", 9.5),
    )

    decision = evaluate(st)

    assert decision.approved is False
    assert decision.verdict == VERDICT_REJECT
    assert decision.checks["critic"]["candidate_id"] == "c2"
    assert decision.checks["critic"]["decision"] is None
    assert any("没有 Critic 评审记录" in r for r in decision.reasons)


def test_resolve_delivered_verdict_without_candidates():
    """没有候选 → (None, None)，不抛。

    `pick_final_candidate` 自己会抛 `AgentIncompleteError`；判据是入口，
    不该因为一个退化状态把整个 tick 炸掉。
    """
    assert resolve_delivered_verdict(state(candidates=[])) == (None, None)


# ---------------------------------------------------------------------------
# 2. 六条判据，每条一个反例
# ---------------------------------------------------------------------------


def test_approves_when_every_criterion_passes():
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1")])

    decision = evaluate(st)

    assert decision.approved is True
    assert decision.verdict == VERDICT_APPROVE
    assert decision.reasons == ()
    # 每条判据都要留下痕迹，哪怕它过了——转正式前要回答"哪条在误拦"，
    # 只知道"被拒了"是回答不了的。
    assert set(decision.checks) == {
        "content_gate",
        "guardrail",
        "critic",
        "critic_score",
        "repetition",
        "volume_cap",
    }


def test_rejects_on_content_gate_violation():
    """判据 1：内容门禁。与发布路径调的是**同一个函数**。"""
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1")])
    leaking = CLEAN + "（见 f1）"

    decision = evaluate(st, content=leaking, fact_ids={"f1"})

    assert decision.approved is False
    assert decision.checks["content_gate"]["violations"] == ["internal_ref_leak"]
    assert any("编号" in r for r in decision.reasons)


def test_rejects_when_decision_is_not_accept():
    """判据 2：交付候选的 Critic 结论必须是 accept。"""
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1", "revise")])

    decision = evaluate(st)

    assert decision.approved is False
    assert decision.checks["critic"]["decision"] == "revise"
    assert any("revise" in r for r in decision.reasons)


def test_rejects_below_min_critic_score():
    """判据 3：分数下限。"""
    st = state(
        candidates=[candidate("c1")],
        critic_results=[verdict("c1", "accept", 6.9)],
    )

    decision = evaluate(st)

    assert decision.approved is False
    assert decision.checks["critic_score"] == {
        "passed": False,
        "value": 6.9,
        "threshold": 7.0,
    }
    # 结论是 accept，所以只该有分数这一条理由——每条反例只触发一条判据。
    assert len(decision.reasons) == 1


def test_rejects_verbatim_repetition():
    """判据 4a：与最近写过的东西有整段相同的原文。"""
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1")])

    decision = evaluate(st, recent_contents=[CLEAN])

    assert decision.approved is False
    assert decision.checks["repetition"]["passed"] is False
    assert {h["layer"] for h in decision.checks["repetition"]["hits"]} == {"verbatim"}


def test_rejects_topic_repetition():
    """判据 4b：同一个题目最近已经写过。影子模式下这一层是主力。"""
    st = state(
        candidates=[candidate("c1")],
        critic_results=[verdict("c1")],
        topic="便宜的推理与注意力",
    )

    decision = evaluate(st, recent_topics=["便宜的推理与注意力"])

    assert decision.approved is False
    assert {h["layer"] for h in decision.checks["repetition"]["hits"]} == {"topic"}


def test_rejects_when_publish_cap_reached():
    """判据 5：当日发布额度。计入**尝试**次数，因为 unknown 那次可能已发出去。"""
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1")])

    decision = evaluate(st, publish_attempts_today=1)

    assert decision.approved is False
    assert decision.checks["volume_cap"]["passed"] is False


def test_zero_publish_cap_blocks_everything():
    """`max_publishes_per_day: 0` 是合法的，含义是"只跑影子，一条都不发"。

    边界在这里容易写反（`<` vs `<=`）：写反的表现是设 0 之后仍然发一条。
    """
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1")])
    decision = evaluate(st, config=config(max_publishes_per_day=0))
    assert decision.approved is False


# ---------------------------------------------------------------------------
# guardrail：为什么不是简单地读 guardrail_passed
# ---------------------------------------------------------------------------


def test_too_long_alone_does_not_block():
    """长度是**我们自己的写作目标**，不是平台的硬限制。

    发布路径早就撤掉了那道 422（账号能发更长），自动审阅必须与它一致：
    一条比目标长一点、其实完全发得出去的正文不该被自己的规则挡在门外。
    直接把 `guardrail_passed` 当判据就会犯这个错——那个布尔值会被 `too_long`
    拉成 False。
    """
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1")])

    decision = evaluate(st, guardrail_reasons=["too_long"])

    assert decision.approved is True
    assert decision.checks["guardrail"]["advisory"] == ["too_long"]
    assert decision.checks["guardrail"]["blocking"] == []


def test_hard_guardrail_reason_blocks():
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1")])

    decision = evaluate(st, guardrail_reasons=["forbidden_content"])

    assert decision.approved is False
    assert decision.checks["guardrail"]["blocking"] == ["forbidden_content"]


def test_unknown_guardrail_reason_blocks_by_default():
    """**方向是刻意的。**

    将来新增一个 guardrail 检查却忘了登记到提示性白名单里时，默认行为是
    "拦住，进人工队列"——那是一个会被发现的、可修的偏差。
    反过来（默认放行）则是静默的：一条新的硬性检查悄悄不生效，没人会知道。
    """
    st = state(candidates=[candidate("c1")], critic_results=[verdict("c1")])

    decision = evaluate(st, guardrail_reasons=["some_future_check"])

    assert decision.approved is False


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


def test_same_input_same_output():
    st = state(
        candidates=[candidate("c1")],
        critic_results=[verdict("c1")],
        topic="题目",
    )
    kwargs = dict(
        recent_contents=["一些别的正文"],
        recent_topics=["另一个题目"],
        publish_attempts_today=0,
    )

    first = evaluate(st, **kwargs)
    second = evaluate(st, **kwargs)

    assert first == second


# ---------------------------------------------------------------------------
# 一致率：None ≠ 0
# ---------------------------------------------------------------------------


def test_summarize_agreement_buckets():
    """三个词**不对等**，映射写错会让一致率虚高。"""
    counts = {
        ("approve", "approve"): 8,  # 一致
        ("approve", "edit"): 1,  # 改稿（正式模式发的正是未编辑原稿）
        ("approve", "reject"): 1,  # 最危险的那个错
        ("reject", "reject"): 6,  # 一致
        ("reject", "approve"): 2,  # 只多两条队列记录
        ("reject", "edit"): 1,  # 人改后发；自动没发，也算误拦
    }

    summary = summarize_agreement(counts)

    assert summary.decided_pairs == 19
    assert summary.agreed == 14
    assert summary.false_approve == 1
    assert summary.soft_mismatch == 1
    assert summary.false_reject == 3
    assert summary.auto_approvals == 10
    assert summary.agreement_rate == 14 / 19
    assert summary.soft_mismatch_rate == 1 / 10


def test_rates_are_none_not_zero_without_data():
    """没有数据时是 `None`。

    "还没人审过"和"审过的全都不同意"在下游是两件事；写成 0 会让界面显示一个
    吓人的数字，而实际含义是"还没有数据"。
    """
    empty = summarize_agreement({})

    assert empty.agreement_rate is None
    assert empty.soft_mismatch_rate is None
    assert empty.decided_pairs == 0


# ---------------------------------------------------------------------------
# 转正式的门槛
# ---------------------------------------------------------------------------


def healthy_summary() -> AgreementSummary:
    return AgreementSummary(
        decided_pairs=25,
        agreed=24,
        false_approve=0,
        soft_mismatch=1,
        false_reject=0,
        auto_approvals=12,
    )


def gate_for(summary, **kwargs):
    kwargs.setdefault("window_days", 10.0)
    kwargs.setdefault("false_reject_reasons", [])
    kwargs.setdefault("stale_claims", 0)
    kwargs.setdefault("auto_approved_clean", True)
    return evaluate_go_live_gate(summary, **kwargs)


def test_gate_passes_on_healthy_data():
    report = gate_for(healthy_summary())

    assert report.ready is True
    assert all(c.met for c in report.criteria)
    # 机器判断不了的那几步**每次都下发**，不会因为自动判据全过就消失。
    assert report.manual_steps


def test_gate_blocks_on_a_single_false_approve():
    """硬阻断：出现一次误放行就必须先查清是哪条判据漏了。"""
    summary = replace(healthy_summary(), false_approve=1)

    report = gate_for(summary)

    assert report.ready is False
    assert not next(c for c in report.criteria if c.key == "false_approve").met


def test_false_approve_criterion_is_not_met_on_zero_samples():
    """**0/0 不是成绩。**

    一条都没自动放行过时，"误放行 = 0" 只说明那条路径还没被测过。把它读成
    "最危险的错从没发生过"，就是在一个什么都没跑过的系统上亮绿灯。

    注意这条与 `test_gate_blocks_when_nothing_has_ever_been_approved` 不是
    一回事：那一条靠 `soft_mismatch`（rate 为 None）把整体拦住，**不会**让
    `false_approve` 这一格变红。所以零样本时这一格曾经是绿的——本条钉的就是它。
    """
    summary = AgreementSummary(
        decided_pairs=0, agreed=0, false_approve=0, soft_mismatch=0, false_reject=0,
        auto_approvals=0,
    )

    criterion = next(c for c in gate_for(summary).criteria if c.key == "false_approve")

    assert criterion.met is False
    assert "什么也没证明" in criterion.detail


def test_false_approve_criterion_is_met_at_the_first_real_approval():
    """边界是 `> 0`，不是"攒够多少条"。

    放过一条、且没被人否决，这一格就该绿。写成 `>= 2` 或别的门槛会把一个
    已经真实发生过的样本继续算成"没有数据"。
    """
    summary = AgreementSummary(
        decided_pairs=1, agreed=1, false_approve=0, soft_mismatch=0, false_reject=0,
        auto_approvals=1,
    )

    criterion = next(c for c in gate_for(summary).criteria if c.key == "false_approve")

    assert criterion.met is True
    assert "没有一次被人否决" in criterion.detail


def test_gate_blocks_when_nothing_has_ever_been_approved():
    """累积了 25 个配对，但一条都没被自动放行过。

    这**不算**通过——正式模式下的批准路径此刻完全没有被测过，
    而转正式唯一放开的就是那条路径。
    """
    summary = AgreementSummary(
        decided_pairs=25, agreed=25, false_approve=0, false_reject=0, auto_approvals=0
    )

    report = gate_for(summary)

    assert report.ready is False
    assert not next(c for c in report.criteria if c.key == "soft_mismatch").met


def test_gate_blocks_on_unattributable_false_reject():
    """一条说不出理由的拒绝无法归因，因此无法被评估。"""
    report = gate_for(healthy_summary(), false_reject_reasons=[[], ["题目重复"]])

    assert report.ready is False
    assert not next(c for c in report.criteria if c.key == "attribution").met


def test_gate_blocks_on_stale_claims():
    report = gate_for(healthy_summary(), stale_claims=2)

    assert report.ready is False
    assert not next(c for c in report.criteria if c.key == "stale_claims").met


def test_gate_blocks_without_enough_days():
    report = gate_for(healthy_summary(), window_days=3.0)

    assert report.ready is False
    assert not next(c for c in report.criteria if c.key == "sample").met
