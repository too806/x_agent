"""学习提案的纯核心测试（Phase 5D）。

这一层的全部风险集中在**一个方向**上：一条建议悄悄变成了一个动作。所以这里
盯的不是"能不能产出提案"，而是四道闸门各自**真的会拦人**：

1. **闸门**：`learning_ready=False` ⇒ 提案数为 0，没有例外；
2. **证据**：没有 `evidence_ids` 的提案直接不成立——本层没有"凭印象"的建议；
3. **目标**：Trend 权重 / Guardrail / 预算 / 发布 / Critic 阈值一律不成立，
   而且拒绝时要**说清楚它踩的是哪条生产边界**；
4. **措辞**：理由永远是观察，不是因果，不是指令，也不是绝对化断言。

另外两条是这一层独有的、很容易写错的：

* `current_value` 是一个**关于生产配置的事实主张**，不是比较的另一侧；
* 版本号必须成对出现，而且**不相等**——相等的两个版本号是"把 A 改成 A"。

真机那半（数据库 + review 端点）在 `tests/test_learning_proposal_api.py`。
这里不碰数据库、不碰 LLM、不取挂钟时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.feedback.schema import PostPerformanceProfile, SnapshotObservation
from app.feedback.signals import (
    MIN_SAMPLES_FOR_COMPARISON,
    find_causal_language,
    find_instruction_language,
)
from app.learning import performance as perf
from app.learning.proposal import (
    ABSOLUTE_MARKERS,
    MAX_PROPOSALS,
    PROPOSAL_ID_PREFIX,
    TARGET_RECIPE_FIELDS,
    generate_proposals,
    proposal_id,
    proposals_by_status,
    validate_proposal,
)
from app.learning.report import build_learning_report
from app.learning.schema import (
    LearningProposal,
    PostPerformanceRecord,
    ProposalStatus,
    ProposalTarget,
)
from app.style.recipe import build_recipe, recipe_version
from app.style.schemas import StyleProfileFeatures
from app.style.taxonomy import HookType, StructureType

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
        structure_type=StructureType.OPINION_FIRST.value,
        hook_type=hook,
        stance="moderate",
        observation_window=window,
        performance=perf.vector_at_window(observations, window),
        observations=observations,
    )


def one_window(
    post_id: str,
    *,
    hook: str,
    likes: int,
    conversation: int = 4,
    amplification: int = 1,
    window: str = WINDOW,
):
    return record(
        post_id,
        hook=hook,
        window=window,
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


def balanced(
    *, question: int = 5, statement: int = 5, likes_high: int = 20, likes_low: int = 8
) -> tuple[PostPerformanceRecord, ...]:
    """5 + 5、三指标齐全、同一个窗口：本层**唯一**该产出提案的形状。"""
    return tuple(
        one_window(f"q{i}", hook="question", likes=likes_high, conversation=12, amplification=6)
        for i in range(question)
    ) + tuple(
        one_window(f"s{i}", hook="statement", likes=likes_low, conversation=4, amplification=1)
        for i in range(statement)
    )


def recipe_for(hook: HookType) -> StyleRecipe:
    profile = StyleProfileFeatures(
        sample_count=40,
        median_length=180.0,
        number_density=1.0,
        emoji_density=0.1,
        structure_distribution={StructureType.OPINION_FIRST.value: 0.6},
        hook_distribution={hook.value: 0.7, "statement": 0.3},
        opening_distribution={},
        closing_distribution={"statement": 0.7},
        sentence_length_distribution={},
    )
    return build_recipe(profile)


def report_for(records) -> object:
    return build_learning_report(records, window=WINDOW)


def proposals_for(records, *, base_recipe=None) -> tuple[LearningProposal, ...]:
    return generate_proposals(report_for(records), base_recipe=base_recipe)


# ---------------------------------------------------------------------------
# 一、闸门：不过就不提
# ---------------------------------------------------------------------------


def test_a_ready_report_produces_exactly_one_proposal():
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.target == ProposalTarget.STYLE_HOOK.value
    assert proposal.status is ProposalStatus.PROPOSED
    assert proposal.sample_count == 10
    assert proposal.observation_window == WINDOW
    assert len(proposal.evidence_ids) == 3  # 三类活动计数各一条


def test_insufficient_sample_produces_no_proposal_at_all():
    report = report_for((one_window("only", hook="question", likes=10),))
    assert report.readiness.learning_ready is False
    assert report.readiness.reason == "insufficient_sample"
    assert generate_proposals(report, base_recipe=recipe_for(HookType.STATEMENT)) == ()


def test_the_same_comparison_does_produce_a_proposal_once_the_gate_opens():
    """反证的另一半，缺了它上一条就是空的。

    只测"样本不足 → 零条"没有意义："零条"完全可以解释成"这批数据本来就推不出
    东西"。这里用同一套比较（同样是 5 打 5、同样三个指标），只是把样本量提上去，
    提案就出现了——所以让上一条归零的确实是**闸门**。
    """
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert len(proposals) == 1


@pytest.mark.parametrize(
    "statement_likes, statement_reposts",
    [
        pytest.param(8, 1, id="three_metrics_agree"),
        pytest.param(8, 9, id="metrics_contradict_each_other"),
        pytest.param(20, 1, id="two_metrics_tie_and_one_differs"),
    ],
)
def test_a_direction_must_be_unanimous_across_the_metrics(
    statement_likes, statement_reposts
):
    """三类活动计数必须**同向**，否则不提案。

    这条是本层刻意的保守：只有点赞说某一侧更高、转发什么也没说（或反过来）
    的时候，"该往哪边试"从这批数据里分不出来。此时挑一个指标来支持一个方向，
    正是 `evals/learning/selftest.py` 里那条"不要只看单一极端值"要挡的事。

    代价是漏报：真实数据里三个计数很少全部同向，所以这一层会**经常**什么都不提。
    这是选对的代价，不是 bug——宁可不提，也不要提一条挑出来的。
    """
    records = tuple(
        one_window(f"q{i}", hook="question", likes=20, conversation=12, amplification=6)
        for i in range(5)
    ) + tuple(
        one_window(
            f"s{i}", hook="statement", likes=statement_likes, conversation=4, amplification=statement_reposts
        )
        for i in range(5)
    )
    report = report_for(records)
    assert report.readiness.learning_ready is True
    proposals = generate_proposals(report, base_recipe=recipe_for(HookType.STATEMENT))
    unanimous = statement_likes == 8 and statement_reposts == 1
    assert bool(proposals) is unanimous, [p.rationale for p in proposals]


def test_a_report_that_mixes_windows_never_produces_a_proposal():
    records = tuple(
        one_window(f"q{i}", hook="question", likes=20) for i in range(5)
    ) + tuple(
        one_window(f"s{i}", hook="statement", likes=8, window="1-6h") for i in range(5)
    )
    report = report_for(records)
    assert report.readiness.learning_ready is False
    assert report.readiness.reason == "inconsistent_window"
    assert generate_proposals(report, base_recipe=recipe_for(HookType.STATEMENT)) == ()


def test_at_most_three_proposals_are_ever_produced():
    """工作单第二十二节：候选提案最多 2-3 条，不许无限扩张。"""
    assert MAX_PROPOSALS == 3
    assert len(proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))) <= MAX_PROPOSALS


# ---------------------------------------------------------------------------
# 二、证据：没有依据的建议不成立
# ---------------------------------------------------------------------------


def _valid_proposal(**overrides) -> LearningProposal:
    """一条合法的提案底稿。改了它才谈得上"因为某处不合法而被拒"。"""
    base = dict(
        id="proposal:hook_type@24-72h",
        target=ProposalTarget.STYLE_HOOK.value,
        current_value=HookType.STATEMENT.value,
        proposed_value=HookType.QUESTION.value,
        rationale=(
            "在 24-72h 观察窗口下，按开头方式分组，question（n=5）的点赞中位数为 20，"
            "statement（n=5）为 8。在本样本中观察到「question」一侧更高。"
            "这是观察到的差异，不构成因果结论；它只说明在下一步里值得尝试把"
            "「开头方式」取 question。"
        ),
        evidence_ids=("learning:hook-type:question:statement:24-72h:attention",),
        sample_count=10,
        observation_window=WINDOW,
        comparison_group="hook_type: question vs statement @ 24-72h · attention",
        limitations=("这是一次观察性比较。",),
    )
    base.update(overrides)
    return LearningProposal(**base)


def test_the_template_is_valid_so_the_negative_cases_mean_something():
    """底稿本身必须合法——否则"改了某处就被拒"什么也说明不了。"""
    assert validate_proposal(_valid_proposal()).valid


def test_a_proposal_without_evidence_is_directly_rejected():
    """工作单第八节：没有 `evidence_ids` 的提案**直接**不成立。"""
    validation = validate_proposal(_valid_proposal(evidence_ids=()))
    assert validation.valid is False
    assert any("没有引用任何 LearningEvidence" in reason for reason in validation.reasons)


def test_a_proposal_whose_evidence_is_not_in_the_report_is_rejected():
    report = report_for(balanced())
    validation = validate_proposal(
        _valid_proposal(evidence_ids=("learning:made-up:nope:24-72h:attention",)),
        report,
    )
    assert validation.valid is False
    assert any("证据" in reason for reason in validation.reasons)


def test_every_generated_proposal_passes_validation_against_its_own_report():
    report = report_for(balanced())
    proposals = generate_proposals(report, base_recipe=recipe_for(HookType.STATEMENT))
    assert proposals
    for proposal in proposals:
        validation = validate_proposal(proposal, report)
        assert validation.valid, validation.reasons


def test_sample_count_is_the_union_of_posts_not_the_sum_of_metric_rows():
    """三条指标看的是**同一批帖子**：n=10，不是 3×10=30。"""
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert proposals[0].sample_count == 10
    assert len(proposals[0].evidence_ids) == 3
    assert proposals[0].sample_count < 3 * 10


# ---------------------------------------------------------------------------
# 三、目标：生产边界永远不接受提案
# ---------------------------------------------------------------------------


def test_every_allowed_target_is_one_of_the_five_enumerated_ones():
    assert {target.value for target in ProposalTarget} == {
        "style_recipe.structure_type",
        "style_recipe.hook_type",
        "style_recipe.target_length",
        "viewpoint.stance",
        "viewpoint.counterpoint_awareness",
    }


@pytest.mark.parametrize(
    "target",
    [
        "trend.weight_recency",
        "trend.weight_novelty",
        "guardrail.max_scope_exceeds_evidence",
        "publish.schedule",
        "publish.auto_publish",
        "budget.daily_limit",
        "critic.threshold",
        "critic.safety_threshold",
        "security.rules",
        "persona.max_length",
        "prompt.writer",
        "review.required",
        "cas.retry_limit",
    ],
)
def test_a_forbidden_target_is_rejected_and_told_which_boundary_it_crossed(target):
    """工作单第二十四、二十六节：越界目标一律 invalid，且**点名**边界。"""
    validation = validate_proposal(_valid_proposal(target=target))
    assert validation.valid is False
    joined = " ".join(validation.reasons)
    assert "安全或生产边界" in joined, validation.reasons
    assert "不能" in joined or "不接受" in joined


@pytest.mark.parametrize(
    "target", ["style_recipe.density", "style_recipe.ending_type", "something.else"]
)
def test_an_unknown_target_is_rejected_without_being_called_a_security_problem(target):
    """越界 ≠ 安全问题：把两者混成一句话，会让人以为密度有什么安全隐患。"""
    validation = validate_proposal(_valid_proposal(target=target))
    assert validation.valid is False
    joined = " ".join(validation.reasons)
    assert "不在允许提案的范围内" in joined
    assert "安全或生产边界" not in joined


def test_generation_only_ever_emits_targets_that_are_allowed():
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    allowed = {target.value for target in ProposalTarget}
    for proposal in proposals:
        assert proposal.target in allowed


# ---------------------------------------------------------------------------
# 四、措辞：观察，不是因果，不是指令
# ---------------------------------------------------------------------------


def test_every_generated_rationale_is_observational():
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert proposals
    for proposal in proposals:
        assert not find_causal_language(proposal.rationale), proposal.rationale
        assert not find_instruction_language(proposal.rationale), proposal.rationale
        assert not [
            marker
            for marker in ABSOLUTE_MARKERS
            if marker in proposal.rationale.casefold()
        ], proposal.rationale
        assert "不构成因果" in proposal.rationale


def test_a_causal_sentence_smuggled_into_a_proposal_is_caught_at_validation():
    """反证：守卫**真的会响**。上一条若只是"恰好没写因果句"，它什么也没守住。"""
    validation = validate_proposal(
        _valid_proposal(rationale="因为用了问句开口，所以点赞中位数更高，这证明该这么做。")
    )
    assert validation.valid is False
    joined = " ".join(validation.reasons)
    assert "因果" in joined


def test_an_instructional_sentence_is_caught_at_validation():
    validation = validate_proposal(
        _valid_proposal(rationale="你应该把开头方式改成 question，请务必照做。")
    )
    assert validation.valid is False
    assert any("指令" in reason for reason in validation.reasons)


def test_an_absolute_claim_is_caught_at_validation():
    validation = validate_proposal(
        _valid_proposal(rationale="改成 question 一定能提高互动，这是必然的。")
    )
    assert validation.valid is False
    assert any("绝对化" in reason for reason in validation.reasons)


# ---------------------------------------------------------------------------
# 五、current_value 说真话的规则
# ---------------------------------------------------------------------------


def test_the_baseline_already_in_effect_produces_no_proposal():
    """"值得尝试把 question 改成 question"是一条没有内容的建议。"""
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.QUESTION))
    assert proposals == ()


def test_current_value_is_the_recipe_field_not_the_lower_side_of_the_comparison():
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert proposals[0].current_value == HookType.STATEMENT.value
    assert proposals[0].proposed_value == HookType.QUESTION.value
    assert proposals[0].current_value != proposals[0].proposed_value


def test_versions_are_paired_and_actually_differ():
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    proposal = proposals[0]
    assert proposal.current_recipe_version is not None
    assert proposal.proposed_recipe_version is not None
    assert proposal.current_recipe_version != proposal.proposed_recipe_version
    assert proposal.current_recipe_version == recipe_version(
        recipe_for(HookType.STATEMENT)
    )


def test_without_a_baseline_there_are_no_versions_and_the_limitation_says_so():
    """没有可信基准时不能编一个版本号——那只是一个看起来精确的假数字。"""
    proposals = proposals_for(balanced(), base_recipe=None)
    assert proposals
    for proposal in proposals:
        assert proposal.current_recipe_version is None
        assert proposal.proposed_recipe_version is None
        assert any("不代表生产配置的当前取值" in item for item in proposal.limitations)


def test_a_target_that_is_not_on_the_recipe_gets_no_version_either():
    """`style_recipe.target_length` 在允许目标里，但 StyleRecipe 上没有这个决策字段。

    这条分支和"没有基准配方"长得像、原因完全不同：这里配方是有的，只是
    这个目标取不到值。两者的说明文字必须不一样，否则读的人会去查一个不存在的问题。
    """
    from app.learning.proposal import _proposed_recipe

    assert "style_recipe.target_length" in {target.value for target in ProposalTarget}
    assert "style_recipe.target_length" not in TARGET_RECIPE_FIELDS
    # 配方在手上，只是这个目标映射不到任何字段 → 版本号为空，而不是拿
    # `observed_target_length` 顶替（那会算出一个"改了长度但没有变化"的版本号）。
    assert _proposed_recipe(recipe_for(HookType.STATEMENT), "style_recipe.target_length", "medium") is None


def test_generation_only_walks_dimensions_that_have_a_valid_comparison():
    """生成路径走的是"哪条比较成立"，不是"允许的目标清单"。

    允许的目标有五个，这份数据里只有 hook_type 能比。若生成路径从目标清单出发，
    就会为另外四个编出提案来——那正是本层最该避免的"为了凑满而提案"。
    """
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert [p.target for p in proposals] == [ProposalTarget.STYLE_HOOK.value]


def test_a_baseline_off_the_compared_pair_is_disclaimed_in_the_limitations():
    """基准取值不在这条比较的两侧时，要说清"这条观察没比较过那个取值"。"""
    # 基准配方取 list（既不是 question 也不是 statement），但它是**真的**取值，
    # 所以版本号照给，只是要点明这条观察没有比较过它。
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.LIST))
    assert proposals
    proposal = proposals[0]
    assert proposal.current_value == HookType.LIST.value
    assert proposal.current_recipe_version is not None
    assert any("不在这条比较的两侧" in item for item in proposal.limitations)


# ---------------------------------------------------------------------------
# 六、结构性保证
# ---------------------------------------------------------------------------


def test_generation_is_deterministic():
    first = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    second = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert [p.model_dump(mode="json") for p in first] == [
        p.model_dump(mode="json") for p in second
    ]


def test_proposal_id_is_deterministic_and_prefixed():
    assert proposal_id("hook_type", WINDOW) == proposal_id("hook_type", WINDOW)
    assert proposal_id("hook_type", WINDOW).startswith(PROPOSAL_ID_PREFIX)
    assert proposal_id("hook_type", WINDOW) != proposal_id("hook_type", "1-6h")


def test_confidence_is_never_invented():
    """工作单第十七节：这一层没有 `proposal_score`，也不编置信度。"""
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert proposals
    for proposal in proposals:
        assert proposal.confidence is None


def test_automatic_application_is_a_field_that_is_always_false():
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    assert proposals
    for proposal in proposals:
        assert proposal.automatic_application is False


def test_proposals_are_never_persisted_by_the_generator():
    """生成是纯函数：它不碰数据库，也没有任何"顺手保存一下"。"""
    import inspect

    from app.learning import proposal as proposal_module

    source = inspect.getsource(proposal_module)
    for forbidden in ("session", "insert", "commit", "sqlalchemy"):
        assert forbidden not in source, f"提案生成里出现了 {forbidden}"


def test_proposals_by_status_groups_without_losing_any():
    proposals = proposals_for(balanced(), base_recipe=recipe_for(HookType.STATEMENT))
    grouped = proposals_by_status(proposals)
    assert grouped[ProposalStatus.PROPOSED] == proposals
    assert sum(len(items) for items in grouped.values()) == len(proposals)


def test_the_shared_threshold_is_reused_rather_than_redeclared():
    """工作单第十八节：`MIN_SAMPLES_FOR_COMPARISON = 5`，不许在提案层降一档。"""
    from app.learning import proposal as proposal_module

    assert proposal_module.MIN_SAMPLES_FOR_COMPARISON is MIN_SAMPLES_FOR_COMPARISON
    assert MIN_SAMPLES_FOR_COMPARISON == 5
