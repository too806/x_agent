"""Agent 质量指标的聚合（Phase 5A.1 第五节）。

纯函数，不需要数据库：输入 RunState，输出一个 pydantic 模型。所以这里能把
"坏数据""边界值"一类的场景钉死，而不必真的跑一轮 Agent。
"""

from datetime import datetime, timezone

from app.agent.budget import AgentBudget
from app.agent.metrics import CRITIC_DECISIONS, finish_reason, quality_metrics
from app.agent.schemas import Candidate, CriticResult, DecisionTraceEntry

BUDGET = AgentBudget(max_steps=12, max_research_calls=4, max_candidates=3, max_revisions=2)


def entry(step: int, action: str, reason: str = "理由") -> DecisionTraceEntry:
    return DecisionTraceEntry(
        step=step, action=action, reason=reason, timestamp=datetime.now(timezone.utc)
    )


def test_metrics_are_read_straight_off_the_state():
    state = {
        "step_count": 9,
        "research_count": 2,
        "revision_count": 1,
        "candidates": [Candidate(candidate_id="c1", content="正文")],
        "critic_tally": {"accept": 2, "revise": 3, "reject": 1},
        "decision_trace": [entry(9, "finish", "评审通过，交给人工")],
    }
    metrics = quality_metrics(state, BUDGET)

    assert metrics.total_steps == 9
    assert metrics.research_calls == 2
    assert metrics.revision_count == 1
    assert metrics.candidate_count == 1
    assert metrics.critic_accept_count == 2
    assert metrics.critic_revise_count == 3
    assert metrics.critic_reject_count == 1
    assert metrics.budget_exhausted is False
    assert metrics.finish_reason == "评审通过，交给人工"


def test_critic_decisions_cover_every_verdict_the_schema_allows():
    """指标里漏一种结论会安静地少算一个数，所以在这里钉死。

    `CriticResult.decision` 是 Literal["accept", "revise", "reject"]，
    指标必须覆盖它的全部取值。
    """
    allowed = CriticResult.model_fields["decision"].annotation
    assert set(CRITIC_DECISIONS) == set(allowed.__args__)


def test_finish_reason_takes_the_last_finish_step():
    """硬停路径上可能出现不止一条 finish 记录，取最后一条。"""
    state = {
        "decision_trace": [
            entry(3, "finish", "模型自己想结束"),
            entry(4, "criticize"),
            entry(12, "finish", "步数与收尾余量均已用尽（12+6），强制结束"),
        ]
    }
    assert finish_reason(state).startswith("步数与收尾余量均已用尽")


def test_finish_reason_is_empty_when_the_run_never_finished():
    """没有理由可读时就返回空串——不替它编一个出来。"""
    assert finish_reason({"decision_trace": [entry(1, "discover_topics")]}) == ""
    assert finish_reason({}) == ""


def test_budget_exhausted_tracks_the_step_budget():
    assert quality_metrics({"step_count": 11}, BUDGET).budget_exhausted is False
    assert quality_metrics({"step_count": 12}, BUDGET).budget_exhausted is True
    assert quality_metrics({"step_count": 17}, BUDGET).budget_exhausted is True


def test_broken_values_do_not_break_the_metrics():
    """state 来自 checkpoint，可能被旧版本或人写坏——坏值不该让控制台 500。"""
    broken = {
        "step_count": "十二",
        "research_count": None,
        "revision_count": True,  # bool 是 int 的子类，不能被当成 1 个计数
        "candidates": None,
        "critic_tally": {"accept": "很多", "revise": 2},
    }
    metrics = quality_metrics(broken, BUDGET)

    assert metrics.total_steps == 0
    assert metrics.research_calls == 0
    assert metrics.revision_count == 0
    assert metrics.candidate_count == 0
    assert metrics.critic_accept_count == 0
    assert metrics.critic_revise_count == 2
    assert metrics.critic_reject_count == 0
    assert metrics.budget_exhausted is False


def test_an_untouched_state_yields_all_zeros():
    assert quality_metrics({}) == quality_metrics({}, BUDGET)
