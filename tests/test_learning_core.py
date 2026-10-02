"""学习准备度层的纯核心测试（Phase 5C）。

这一层最容易写坏的三处，各有一组测试盯着：

1. **分析单位**：一个帖子在一个窗口里只贡献一个值，`sample_count` 是帖子数；
2. **NULL ≠ 0**：读不到的计数不进分布，也不变成 0；
3. **不许说结论**：比较只在成立时产出，措辞永远是观察，出口会拦下因果句。

真机那半（数据库 + checkpoint → 报告，以及 Admin API）在
`tests/test_learning_api.py` 里。这里不碰数据库。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agent.schemas import Angle, ViewpointStance
from app.feedback.schema import PostPerformanceProfile, SnapshotObservation
from app.feedback.signals import MIN_SAMPLES_FOR_COMPARISON, InstructionalLanguageError
from app.learning import performance as perf
from app.learning.cohort import (
    build_cohorts,
    cohort_stats,
    compare_cohorts,
    dimension_value,
)
from app.learning.evidence import evidence_id
from app.learning.report import LearningReportError, _validate, build_learning_report
from app.learning.runtime import record_from_profile
from app.learning.schema import (
    CohortDimension,
    LearningEvidenceKind,
    PerformanceMetric,
    PostPerformanceRecord,
    ReadinessCheck,
)

PUBLISHED_AT = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
WINDOW = "24-72h"


def observation(
    snapshot_id: str,
    hours: float,
    *,
    likes: int | None = None,
    replies: int | None = None,
    reposts: int | None = None,
    quotes: int | None = None,
) -> SnapshotObservation:
    return SnapshotObservation(
        snapshot_id=snapshot_id,
        x_post_id=f"x-{snapshot_id}",
        published_post_id=f"post-{snapshot_id}",
        observed_at=PUBLISHED_AT + timedelta(hours=hours),
        hours_since_publish=hours,
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
    )


def record(
    post_id: str,
    *,
    observations: tuple[SnapshotObservation, ...],
    hook: str = "question",
    window: str = WINDOW,
) -> PostPerformanceRecord:
    return PostPerformanceRecord(
        published_post_id=post_id,
        x_post_id=f"x-{post_id}",
        published_at=PUBLISHED_AT,
        topic="同一个选题",
        structure_type="opinion_first",
        hook_type=hook,
        stance=ViewpointStance.MODERATE.value,
        observation_window=window,
        performance=perf.vector_at_window(observations, window),
        observations=observations,
    )


def one_window(post_id: str, *, hook: str, likes: int, conversation: int = 4, amplification: int = 1):
    return record(
        post_id,
        hook=hook,
        observations=(
            observation(
                f"{post_id}-1",
                48.0,
                likes=likes,
                replies=conversation,
                reposts=amplification,
                quotes=0,
            ),
        ),
    )


def balanced(*, question: int = 5, statement: int = 5) -> tuple[PostPerformanceRecord, ...]:
    return tuple(
        one_window(f"q{i}", hook="question", likes=20, conversation=12, amplification=6)
        for i in range(question)
    ) + tuple(
        one_window(f"s{i}", hook="statement", likes=8, conversation=4, amplification=1)
        for i in range(statement)
    )


# ---------------------------------------------------------------------------
# 窗口取值：分析单位是帖子
# ---------------------------------------------------------------------------


def test_window_takes_the_latest_observation_inside_it():
    """窗口内取**最新**一条，不是第一条：计数是累计的。"""
    observations = (
        observation("a", 25.0, likes=3, replies=0, reposts=0, quotes=0),
        observation("b", 47.0, likes=9, replies=0, reposts=0, quotes=0),
        observation("c", 50.0, likes=11, replies=0, reposts=0, quotes=0),
    )
    assert perf.vector_at_window(observations, WINDOW).attention == 11
    picked = perf.select_window_observation(observations, WINDOW)
    assert picked is not None and picked.snapshot_id == "c"


def test_window_without_observations_is_empty_not_zero():
    """窗口内没有观察 → 三个分量全是 `None`。**不是 0**。"""
    observations = (observation("a", 3.0, likes=40, replies=1, reposts=1, quotes=1),)
    vector = perf.vector_at_window(observations, WINDOW)
    assert vector.is_empty
    assert vector.attention is None
    assert vector.conversation is None
    assert vector.amplification is None


def test_observations_outside_any_window_are_dropped_from_the_vector():
    """`hours_since_publish` 缺失或为负 → 不属于任何窗口，不进向量。"""
    assert perf.vector_at_window((observation("a", -3.0, likes=5),), "0-1h").is_empty


def test_one_post_with_many_snapshots_is_still_one_sample():
    """本层最核心的一条：快照不是样本。"""
    observations = tuple(
        observation(f"s{i}", 24.0 + i, likes=10 + i) for i in range(6)
    )
    report = build_learning_report((record("p0", observations=observations),), window=WINDOW)
    assert len(report.records[0].observations) == 6
    assert report.readiness.sample_count == 1
    assert report.readiness.reason == "insufficient_sample"


def test_dominant_window_prefers_the_window_with_more_posts_and_breaks_ties_in_label_order():
    early = (observation("e", 3.0, likes=1),)
    late = (observation("l", 48.0, likes=1),)
    assert perf.dominant_window([early, late, late]) == WINDOW
    # 并列时按固定顺序取靠前的那个——结果不随遍历顺序漂。
    assert perf.dominant_window([late, early]) == "1-6h"
    assert perf.dominant_window([]) == perf.WINDOW_LABELS[0]


def test_length_buckets_are_closed_and_unknown_stays_none():
    assert perf.length_bucket(None) is None
    assert perf.length_bucket(perf.LENGTH_BUCKET_SHORT_MAX) == "short"
    assert perf.length_bucket(perf.LENGTH_BUCKET_SHORT_MAX + 1) == "medium"
    assert perf.length_bucket(perf.LENGTH_BUCKET_MEDIUM_MAX + 1) == "long"


# ---------------------------------------------------------------------------
# NULL 与离群点
# ---------------------------------------------------------------------------


def test_a_snapshot_missing_a_count_keeps_that_component_none():
    """`replies` 与 `quotes` 都读不到 → 对话是 `None`，不是 0。"""
    vector = perf.vector_at_window((observation("a", 48.0, likes=7),), WINDOW)
    assert vector.attention == 7
    assert vector.conversation is None
    assert vector.amplification is None


def test_cohort_stats_exclude_posts_without_a_reading():
    records = (
        one_window("a", hook="question", likes=10, conversation=2),
        one_window("b", hook="question", likes=12, conversation=2),
        record(
            "c",
            hook="question",
            observations=(observation("c-1", 48.0, likes=None, replies=None),),
        ),
    )
    cohorts = build_cohorts(records, CohortDimension.HOOK_TYPE, window=WINDOW)
    assert len(cohorts) == 1 and cohorts[0].sample_count == 3
    stats = cohort_stats(records, cohorts[0], PerformanceMetric.ATTENTION)
    # 三条帖子，只有两条有注意力读数——第三条**不补 0**，也不进样本。
    assert stats.sample_count == 2
    assert stats.median == 11.0
    assert stats.minimum == 10.0


def test_percentiles_interpolate_and_a_single_reading_is_itself():
    assert perf.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert perf.percentile([1, 2, 3, 4], 0.25) == 1.75
    assert perf.percentile([7], 0.5) == 7.0
    assert perf.percentile([], 0.5) is None
    assert perf.distribution([]).median is None


def test_outliers_are_flagged_but_never_removed():
    values = [10.0, 11.0, 12.0, 13.0, 900.0]
    flags = perf.mark_outliers(values)
    assert flags == (False, False, False, False, True)
    dist = perf.distribution(values)
    assert dist.sample_count == 5
    assert dist.median == 12.0
    assert dist.maximum == 900.0
    # 样本太小就不判离群：栅栏这时几乎完全由端点决定。
    assert perf.mark_outliers([1.0, 2.0, 900.0]) == (False, False, False)


# ---------------------------------------------------------------------------
# 分组与比较
# ---------------------------------------------------------------------------


def test_unclassified_values_do_not_form_a_comparison_group():
    """`unclassified` 不是"另一种写法"，它是"没有分类"——不与别组比较。"""
    records = tuple(
        one_window(f"q{i}", hook="question", likes=20) for i in range(5)
    ) + tuple(one_window(f"a{i}", hook="unclassified", likes=5) for i in range(5))
    cohorts = build_cohorts(records, CohortDimension.HOOK_TYPE, window=WINDOW)
    assert {c.value for c in cohorts} == {"question", "unclassified"}
    comparisons = compare_cohorts(
        records,
        cohorts,
        dimension=CohortDimension.HOOK_TYPE,
        window=WINDOW,
        metric=PerformanceMetric.ATTENTION,
    )
    assert len(comparisons) == 1
    assert comparisons[0].valid is False
    assert "没有可用的分类" in (comparisons[0].invalid_reason or "")
    assert "不构成可比" in comparisons[0].statement


def test_records_from_another_window_never_join_a_cohort():
    """cohort 的 `observation_window` 是对**全部成员**的陈述，不能是假的。"""
    records = tuple(one_window(f"q{i}", hook="question", likes=20) for i in range(3)) + tuple(
        record(
            f"e{i}",
            hook="question",
            window="1-6h",
            observations=(observation(f"e{i}-1", 3.0, likes=20),),
        )
        for i in range(3)
    )
    cohorts = build_cohorts(records, CohortDimension.HOOK_TYPE, window=WINDOW)
    assert len(cohorts) == 1
    assert cohorts[0].sample_count == 3
    assert all(pid.startswith("q") for pid in cohorts[0].post_ids)


def test_a_small_group_produces_a_reason_instead_of_a_conclusion():
    records = balanced(question=5, statement=3)
    report = build_learning_report(records, window=WINDOW)
    assert report.readiness.reason == "invalid_comparison"
    assert report.valid_comparisons == ()
    assert report.comparisons
    for comparison in report.comparisons:
        assert comparison.valid is False
        assert comparison.observed_difference is None
        assert comparison.invalid_reason
        assert "因此" not in comparison.statement


def test_cohorts_carry_the_three_things_that_make_a_comparison_answerable():
    report = build_learning_report(balanced(), window=WINDOW)
    assert report.cohorts
    for cohort in report.cohorts:
        assert cohort.sample_count > 0
        assert cohort.observation_window == WINDOW
        assert cohort.selection_rule
        assert "帖子" in cohort.selection_rule


def test_equal_medians_do_not_get_a_direction():
    """两组中位数相等时不许说某一方"更高"——那是在编一个不存在的差异。"""
    records = tuple(
        one_window(f"q{i}", hook="question", likes=20, conversation=12, amplification=6)
        for i in range(5)
    ) + tuple(
        one_window(f"s{i}", hook="statement", likes=20, conversation=12, amplification=6)
        for i in range(5)
    )
    cohorts = build_cohorts(records, CohortDimension.HOOK_TYPE, window=WINDOW)
    comparisons = compare_cohorts(
        records,
        cohorts,
        dimension=CohortDimension.HOOK_TYPE,
        window=WINDOW,
        metric=PerformanceMetric.ATTENTION,
    )
    comparison = comparisons[0]
    assert comparison.valid is True
    assert comparison.observed_difference == 0
    assert "更高" not in comparison.statement
    assert "中位数相同" in comparison.statement


def test_comparison_evidence_id_is_deterministic_and_readable():
    report = build_learning_report(balanced(), window=WINDOW)
    first = [evidence_id(c) for c in report.valid_comparisons]
    second = [evidence_id(c) for c in build_learning_report(balanced(), window=WINDOW).valid_comparisons]
    assert first == second
    assert all(item.startswith("learning:") for item in first)
    assert all(":" in item for item in first)


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------


def test_gate_opens_only_when_all_four_conditions_hold():
    report = build_learning_report(balanced(), window=WINDOW)
    assert report.readiness.learning_ready is True
    assert report.readiness.reason == "ready"
    assert [check.name for check in report.readiness.checks] == [
        "sample_count",
        "window_consistent",
        "metrics_present",
        "comparison_valid",
    ]
    assert all(check.passed for check in report.readiness.checks)
    # "可以作为学习材料"永远不等于"可以改 Agent"。
    assert report.readiness.automatic_modification is False
    assert report.readiness.usable_for_future_learning is True


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        ((one_window("a", hook="question", likes=1),), "insufficient_sample"),
        (
            balanced(question=5, statement=3),
            "invalid_comparison",
        ),
    ],
)
def test_gate_reasons_are_reported_in_priority_order(records, reason):
    report = build_learning_report(records, window=WINDOW)
    assert report.readiness.learning_ready is False
    assert report.readiness.reason == reason
    kinds = {item.type for item in report.evidence}
    assert kinds != {LearningEvidenceKind.COHORT_DIFFERENCE}
    assert len(report.evidence) == 1
    assert report.evidence[0].comparison_group is None


def test_gate_refuses_a_report_that_mixes_windows():
    records = tuple(one_window(f"q{i}", hook="question", likes=20) for i in range(5)) + tuple(
        record(
            f"e{i}",
            hook="statement",
            window="1-6h",
            observations=(observation(f"e{i}-1", 3.0, likes=5),),
        )
        for i in range(5)
    )
    report = build_learning_report(records, window=WINDOW)
    assert report.readiness.reason == "inconsistent_window"
    assert report.valid_comparisons == ()
    assert any("窗口" in note for note in report.readiness.limitations)


def test_gate_reports_missing_metrics_without_inventing_zeros():
    records = []
    for index, metric in enumerate(("likes", "conversation", "amplification")):
        for sub in range(2):
            values = {"likes": None, "replies": None, "reposts": None, "quotes": None}
            if metric == "likes":
                values["likes"] = 9 + sub
            elif metric == "conversation":
                values["replies"] = 4 + sub
                values["quotes"] = 0
            else:
                values["reposts"] = 2 + sub
            records.append(
                record(
                    f"p{index}{sub}",
                    hook="question",
                    observations=(observation(f"p{index}{sub}-1", 48.0, **values),),
                )
            )
    report = build_learning_report(tuple(records), window=WINDOW)
    assert report.readiness.reason == "missing_metrics"
    detail = next(c.detail for c in report.readiness.checks if c.name == "metrics_present")
    assert "attention=2" in detail and "conversation=2" in detail


def test_readiness_check_model_is_the_only_place_the_gate_decides():
    """闸门是四个显式布尔，不接受一个"总开关"式的输入。"""
    report = build_learning_report(balanced(), window=WINDOW)
    readiness = report.readiness
    assert all(isinstance(check, ReadinessCheck) for check in readiness.checks)
    assert readiness.sample_count >= MIN_SAMPLES_FOR_COMPARISON


# ---------------------------------------------------------------------------
# 出口校验
# ---------------------------------------------------------------------------


def test_report_rejects_a_causal_statement_at_the_exit():
    report = build_learning_report(balanced(), window=WINDOW)
    poisoned = report.model_copy(
        update={
            "comparisons": tuple(
                item.model_copy(update={"statement": "因为开头是问句，所以点赞更多。"})
                for item in report.comparisons
            )
        }
    )
    with pytest.raises(LearningReportError):
        _validate(poisoned)


def test_report_rejects_an_instructional_statement_at_the_exit():
    report = build_learning_report(balanced(), window=WINDOW)
    poisoned = report.model_copy(
        update={
            "evidence": tuple(
                item.model_copy(update={"statement": "建议下次都用问句开头。"})
                for item in report.evidence
            )
        }
    )
    with pytest.raises(LearningReportError):
        _validate(poisoned)


def test_report_rejects_an_invented_confidence():
    report = build_learning_report(balanced(), window=WINDOW)
    poisoned = report.model_copy(
        update={
            "evidence": tuple(
                item.model_copy(update={"confidence": 0.9}) for item in report.evidence
            )
        }
    )
    with pytest.raises(LearningReportError):
        _validate(poisoned)


def test_report_rejects_evidence_that_points_at_a_missing_observation():
    report = build_learning_report(balanced(), window=WINDOW)
    poisoned = report.model_copy(
        update={
            "evidence": tuple(
                item.model_copy(update={"source_snapshot_ids": ("snap-不存在",)})
                for item in report.evidence
            )
        }
    )
    with pytest.raises(LearningReportError):
        _validate(poisoned)


def test_every_evidence_carries_its_traceability_chain():
    report = build_learning_report(balanced(), window=WINDOW)
    known_snapshots = {
        obs.snapshot_id for item in report.records for obs in item.observations
    }
    known_posts = {item.published_post_id for item in report.records}
    known_x = {item.x_post_id for item in report.records}
    assert report.evidence
    for item in report.evidence:
        assert set(item.source_snapshot_ids) <= known_snapshots
        assert set(item.source_post_ids) <= known_posts
        assert set(item.source_x_post_ids) <= known_x
        assert item.confidence is None
        assert item.statement


def test_a_report_without_records_says_so_instead_of_returning_zeros():
    report = build_learning_report((), window=WINDOW)
    assert report.records == ()
    assert report.comparisons == ()
    assert report.readiness.learning_ready is False
    assert report.readiness.reason == "insufficient_sample"
    assert any("没有任何已发布帖子" in note for note in report.limitations)
    assert report.evidence and report.evidence[0].type is LearningEvidenceKind.INSUFFICIENT_SAMPLE


def test_limitations_are_derived_from_this_data_not_a_fixed_paragraph():
    """没算的要说出来，而且要说**这一份数据**的缺口，不是一段模板文案。"""
    bare = build_learning_report(
        (
            PostPerformanceRecord(
                published_post_id="p", observation_window=WINDOW, performance=perf.PerformanceVector(attention=1)
            ),
        ),
        window=WINDOW,
    )
    joined = " ".join(bare.limitations)
    assert "topic class" in joined
    assert "写作结构" in joined or "长度" in joined


# ---------------------------------------------------------------------------
# 归因 → 记录
# ---------------------------------------------------------------------------


def test_record_from_profile_maps_the_three_blocks_without_recomputing_anything():
    profile = PostPerformanceProfile(
        published_post_id="p1",
        x_post_id="x1",
        published_at=PUBLISHED_AT,
        content_length=100,
        run_id="r1",
        topic="选题",
        selected_topic="选题",
        topic_trend_score=12.5,
        selected_angle_claim="判断",
        selected_angle_confidence=0.6,
        viewpoint_stance=ViewpointStance.MODERATE.value,
        counterpoint_awareness=True,
        scope_exceeds_evidence=False,
        style_structure="opinion_first",
        style_hook="question",
        style_target_length=140,
        critic_score=8.0,
        observations=(observation("s1", 48.0, likes=10, replies=2, reposts=1, quotes=1),),
        limitations=("局限",),
    )
    item = record_from_profile(profile, window=WINDOW)
    assert item.published_post_id == "p1"
    assert item.trend_score == 12.5
    assert item.structure_type == "opinion_first"
    assert item.hook_type == "question"
    assert item.stance == ViewpointStance.MODERATE.value
    assert item.counterpoint_awareness is True
    assert item.scope_exceeds_evidence is False
    assert item.critic_score == 8.0
    assert item.length_bucket == "medium"
    assert item.performance.attention == 10
    assert item.performance.conversation == 3
    assert item.performance.amplification == 1
    assert item.limitations == ("局限",)


def test_missing_viewpoint_fields_stay_none_instead_of_false():
    """读不到当时的观点 → 三个字段都是 `None`，**不是 `False`**。

    `False` 是"当时确实没有反方视角"（一条真实的观察），两者含义相反。
    """
    profile = PostPerformanceProfile(published_post_id="p1", x_post_id="x1")
    item = record_from_profile(profile, window=WINDOW)
    assert item.stance is None
    assert item.counterpoint_awareness is None
    assert item.scope_exceeds_evidence is None
    assert item.length_bucket is None
    assert item.performance.is_empty


def test_dimension_values_come_from_the_record_and_none_is_not_a_bucket():
    item = record_from_profile(PostPerformanceProfile(published_post_id="p"), window=WINDOW)
    assert dimension_value(item, CohortDimension.HOOK_TYPE) is None
    assert dimension_value(item, CohortDimension.COUNTERPOINT_AWARENESS) is None
    full = record_from_profile(
        PostPerformanceProfile(
            published_post_id="p",
            viewpoint_stance="moderate",
            counterpoint_awareness=False,
            scope_exceeds_evidence=True,
        ),
        window=WINDOW,
    )
    assert dimension_value(full, CohortDimension.COUNTERPOINT_AWARENESS) == "no"
    assert dimension_value(full, CohortDimension.SCOPE_EXCEEDS_EVIDENCE) == "yes"


# ---------------------------------------------------------------------------
# Feedback Context 的接口升级（工作单第二十一至二十三节）
# ---------------------------------------------------------------------------


def test_the_learning_context_is_an_interface_and_is_not_wired_into_any_template():
    """本阶段只留接口：没有 `{{LEARNING}}`，候选工具也不组装它。"""
    from pathlib import Path

    from app.agent.prompts import format_learning_context
    from app.config import settings

    prompt_dir = Path(settings.agent_prompt_dir)
    templates = sorted(prompt_dir.glob("*.txt"))
    assert templates, "prompt 目录里一个模板都没有，这条检查就没有意义"
    for path in templates:
        text = path.read_text(encoding="utf-8")
        assert "{{LEARNING}}" not in text, f"{path.name} 已经接入了学习上下文"

    from app.agent.tools import candidates as candidates_tool

    source = Path(candidates_tool.__file__).read_text(encoding="utf-8")
    assert "format_learning_context" not in source
    assert "learning" not in source.lower()

    assert format_learning_context(None).strip()
    assert "没有可用" in format_learning_context(None)


def test_the_learning_context_never_issues_instructions_or_claims():
    from app.agent.prompts import format_learning_context
    from app.feedback.signals import (
        CAUSAL_MARKERS,
        INSTRUCTION_MARKERS,
        assert_observational,
    )

    text = format_learning_context(build_learning_report(balanced(), window=WINDOW))
    assert text.strip()
    # 先确认它**真的**是一份摘要，而不是守卫失败后替换掉的丢弃文案——
    # 否则下面那圈"没有出现坏词"的断言会因为内容为空而恒真。
    assert "学习准备度" in text
    assert "观察窗口" in text
    for marker in INSTRUCTION_MARKERS + CAUSAL_MARKERS:
        assert marker not in text, f"学习上下文里出现了 {marker!r}"
    assert_observational(text, where="learning context")

    denied = format_learning_context(build_learning_report((), window=WINDOW))
    for marker in INSTRUCTION_MARKERS + CAUSAL_MARKERS:
        assert marker not in denied


def test_the_instructional_guard_would_have_caught_a_written_instruction():
    """反证：守卫真的会响——否则上面两条是空的。"""
    from app.feedback.signals import assert_observational

    with pytest.raises(InstructionalLanguageError):
        assert_observational("你应该多用问句开头。", where="test")


def test_writer_prompt_states_that_feedback_is_neither_fact_nor_guarantee():
    """工作单第二十三节：那一句必须真的在模板里。"""
    from app.agent.prompts import load_prompt

    template = load_prompt("candidates")
    feedback_block = template.split("{{FEEDBACK}}")[0].split("{{STYLE}}")[-1]
    assert "不是写作指令" in feedback_block
    assert "不是已证实的事实" in feedback_block
    assert "不能保证" in feedback_block


# ---------------------------------------------------------------------------
# 角度 → 归因的两个新字段（Phase 5C 的加法）
# ---------------------------------------------------------------------------


def test_counterpoint_awareness_distinguishes_absent_from_unknown():
    from app.feedback.profile import _counterpoint_awareness, _scope_exceeds_evidence

    assert _counterpoint_awareness(None) is None
    assert _counterpoint_awareness(Angle(angle_id="a", claim="c", counterpoints=[])) is False
    assert _counterpoint_awareness(Angle(angle_id="a", claim="c", counterpoints=["反方"])) is True
    assert _scope_exceeds_evidence(None) is None
    assert _scope_exceeds_evidence(Angle(angle_id="a", claim="c")) is None


async def test_profile_projects_the_viewpoint_fields_from_the_checkpoint(session_factory):
    """真机那半：checkpoint 里的 Angle → 归因里的三个观点字段。

    这三个字段是 Phase 5C 的**加法**（第五层需要 stance / 反方意识 / 范围作为
    分组维度），来源只能是 checkpoint 里当场记下的 Angle。所以这条测试断的是
    "投影真的发生了"，而不是"字段存在"。
    """
    from fakes import StubGraph

    from app.feedback.profile import build_post_performance_profile
    from app.models import Draft, PublishedPost
    from app.models.published_post import STATUS_PUBLISHED
    from app.repositories import create_agent_run

    angle = Angle(
        angle_id="a1",
        claim="一个判断",
        counterpoints=["反方一"],
        scope_exceeds_evidence=True,
        stance=ViewpointStance.CAUTIOUS,
    )
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        draft = Draft(agent_run_id=run.id, content="正文", status="published", version=1)
        session.add(draft)
        await session.flush()
        post = PublishedPost(
            draft_id=draft.id,
            agent_run_id=run.id,
            body="正文",
            status=STATUS_PUBLISHED,
            x_post_id="1900",
            published_at=PUBLISHED_AT,
        )
        session.add(post)
        await session.commit()
        post_id = str(post.id)

    graph = StubGraph({"selected_topic": None, "selected_angle": angle})
    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, graph, published_post_id=post_id
        )

    assert profile is not None
    assert profile.viewpoint_stance == ViewpointStance.CAUTIOUS.value
    assert profile.counterpoint_awareness is True
    assert profile.scope_exceeds_evidence is True
    # 这条帖子一条快照都没有：窗口取不到值，三个分量必须是 None 而不是 0。
    assert profile.observations == ()
