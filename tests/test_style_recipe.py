"""`app/style/recipe.py`：Profile + 证据信号 → StyleRecipe。

这一层是"风格"第一次变成**可执行的约束**。所以测的重点不是"选得对不对"
（那是分类法收敛的事，要靠真实样本），而是三件能被机器钉死的事：

1. **确定性**——同一个 Profile 必然得到同一个 recipe，包括并列时的取舍；
2. **每个选择都有依据**——`reasons` 非空，且"因为 X 所以 Y"里的 X 真的成立；
3. **降级是保守的**——没有数据时退回中性约束，绝不凭空要求内容。
"""

from __future__ import annotations

import pytest

from app.agent.schemas import EvidenceFact, EvidencePack
from app.style.config import load_style_config
from app.style.recipe import EvidenceSignals, build_recipe, evidence_signals
from app.style.schemas import (
    DensityLevel,
    EndingType,
    EvidenceRequirement,
    StanceStrength,
    StyleProfileFeatures,
    Tone,
)
from app.style.taxonomy import HookType, StructureType

CFG = load_style_config()


def profile(**overrides) -> StyleProfileFeatures:
    """一个"有数据"的 Profile，各字段可覆盖。"""
    base = dict(
        sample_count=12,
        median_length=220.0,
        number_density=2.0,
        emoji_density=0.0,
        structure_distribution={StructureType.OPINION_FIRST.value: 1.0},
        hook_distribution={HookType.QUESTION.value: 1.0},
        closing_distribution={"statement": 1.0},
    )
    base.update(overrides)
    return StyleProfileFeatures(**base)


def signals(**overrides) -> EvidenceSignals:
    base = dict(fact_count=3, counterpoint_count=0, has_numeric_fact=False, unknown_count=0)
    base.update(overrides)
    return EvidenceSignals(**base)


# ---------------------------------------------------------------------------
# 没有 Profile 时：中性 recipe，且如实说明
# ---------------------------------------------------------------------------


def test_no_profile_yields_a_neutral_recipe_not_none():
    """刻意返回中性 recipe 而不是 `None`。

    调用方拿到中性 recipe 与拿到 None 会导致两条不同的代码路径，而其中
    一条（无 recipe）正是 Smoke 4 要保住的老行为。让"没有 Profile"在
    recipe 内部表达，调用方就只有一条路径。
    """
    recipe = build_recipe(None)
    assert recipe.structure_type is StructureType.UNCLASSIFIED
    assert recipe.hook_type is HookType.UNCLASSIFIED
    assert recipe.observed_target_length is None
    assert recipe.reasons  # 必须说明为什么没约束


def test_empty_profile_also_degrades_to_neutral():
    """`sample_count == 0` 与 `None` 同义：都没有可依据的统计量。"""
    recipe = build_recipe(profile(sample_count=0, median_length=None))
    assert recipe.structure_type is StructureType.UNCLASSIFIED
    assert recipe.observed_target_length is None
    assert any("未取得" in reason for reason in recipe.reasons)


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


def test_same_inputs_yield_the_same_recipe():
    a = build_recipe(profile(), signals(), config=CFG, profile_ref="corpus@T")
    b = build_recipe(profile(), signals(), config=CFG, profile_ref="corpus@T")
    assert a.model_dump() == b.model_dump()


def test_ties_are_broken_deterministically():
    """并列时取键序最小的那一项。

    这条挡的是"字典遍历顺序影响结果"：一个五五开的结构分布如果按插入顺序
    选，同一份 Profile 会在不同进程里给出不同的 recipe，而原因看不见。
    """
    tie = {
        StructureType.OPINION_FIRST.value: 0.5,
        StructureType.CONTRAST.value: 0.5,
    }
    first = build_recipe(profile(structure_distribution=tie), signals(), config=CFG)
    second = build_recipe(
        profile(structure_distribution=dict(reversed(list(tie.items())))), signals(), config=CFG
    )
    assert first.structure_type is second.structure_type is StructureType.CONTRAST


def test_a_stale_label_from_an_older_taxonomy_does_not_crash():
    """分类法收敛过程中语料里可能留着旧标签，归成 unclassified 而不是抛。

    一个陈旧标签不该让整轮运行失败——那是一次采集问题，不是运行时事故。
    """
    recipe = build_recipe(
        profile(structure_distribution={"opinion_first_v0": 1.0}),
        signals(),
        config=CFG,
    )
    assert recipe.structure_type is StructureType.UNCLASSIFIED


# ---------------------------------------------------------------------------
# recipe 是结构化的
# ---------------------------------------------------------------------------


def test_recipe_fields_are_all_typed_constraints():
    """每个字段都必须是枚举成员/整数，**没有自由文本**（除 reasons/profile_ref）。

    这条是"recipe 是约束不是正文"的类型层断言：一旦有人加一个装原文的
    字符串字段，Writer 就可能开始抄它。
    """
    recipe = build_recipe(profile(), signals(fact_count=5, has_numeric_fact=True), config=CFG)
    assert isinstance(recipe.structure_type, StructureType)
    assert isinstance(recipe.hook_type, HookType)
    assert isinstance(recipe.density, DensityLevel)
    assert isinstance(recipe.stance_strength, StanceStrength)
    assert isinstance(recipe.evidence_requirement, EvidenceRequirement)
    assert isinstance(recipe.tone, Tone)
    assert isinstance(recipe.ending_type, EndingType)
    assert isinstance(recipe.observed_target_length, int)


def test_target_length_is_clamped_into_the_configured_range():
    """中位数收敛到配置区间：约束必须可执行，不能是"写 5000 字"。"""
    low = build_recipe(profile(median_length=10.0), signals(), config=CFG)
    high = build_recipe(profile(median_length=9000.0), signals(), config=CFG)
    assert low.observed_target_length == CFG.recipe.min_target_length
    assert high.observed_target_length == CFG.recipe.max_target_length


# ---------------------------------------------------------------------------
# 语料目标 / 平台硬上限 / 执行值：三个数，三件事
#
# 这一组守的是一个具体的误导。语料说"这类内容典型 180 字"，平台只让发
# 140 字——如果这两个数被压成一个字段，Run Detail 上就只剩一个 140，
# 而任何人都会把它读成"我们学到的写法就是写 140 字"。于是 40 个字的信息量
# 是被平台的容量限制删掉的这件事，变成了一条关于语料的（错误）结论。
# ---------------------------------------------------------------------------


def test_the_corpus_target_is_never_rewritten_by_the_platform_cap():
    """`observed_target_length` 只反映语料，**不**被平台上限改写。"""
    recipe = build_recipe(
        profile(median_length=180.0), signals(), config=CFG, hard_max_length=140
    )
    assert recipe.observed_target_length == 180, "语料目标被平台上限改写了"
    assert recipe.hard_max_length == 140


def test_the_effective_length_is_the_smaller_of_the_two():
    recipe = build_recipe(
        profile(median_length=180.0), signals(), config=CFG, hard_max_length=140
    )
    assert recipe.effective_target_length == 140
    assert recipe.is_length_capped is True


def test_a_roomy_cap_changes_nothing():
    """上限比语料目标大时，执行值就是语料目标，且**不**解释一次并不存在的收缩。"""
    recipe = build_recipe(
        profile(median_length=180.0), signals(), config=CFG, hard_max_length=1000
    )
    assert recipe.effective_target_length == 180
    assert recipe.is_length_capped is False
    assert not any("硬上限" in reason for reason in recipe.reasons)


def test_a_capped_length_is_explained_in_the_reasons():
    """收缩**必须**说出来，否则那个 140 看起来像语料的结论。

    这条断言的是"解释里同时出现了两个数"：只提上限、不提语料目标，
    读的人仍然不知道被砍掉了多少。
    """
    recipe = build_recipe(
        profile(median_length=180.0), signals(), config=CFG, hard_max_length=140
    )
    explanation = [r for r in recipe.reasons if "140" in r]
    assert explanation, "长度被硬上限压过，reasons 里却没有任何一句提到这次收缩"
    assert any("180" in r for r in explanation), "解释里没有出现语料目标，人看不出被砍掉了多少"


def test_without_a_corpus_length_the_cap_does_not_become_the_target():
    """没有语料依据时不得拿硬上限冒充目标长度。

    `observed_target_length` 为 `None` 表示"语料没有可用的长度分布"，
    而**不是**"目标就是上限"。返回上限会让 Writer 收到一个语料从未
    支持过的长度，而它在报告里与"语料说该写这么长"长得一模一样。
    """
    recipe = build_recipe(
        profile(sample_count=0, median_length=None), signals(), config=CFG,
        hard_max_length=140,
    )
    assert recipe.observed_target_length is None
    assert recipe.effective_target_length is None
    assert recipe.is_length_capped is False


def test_the_effective_length_is_derived_not_stored():
    """执行值是**推出来的**，不是存下来的第二个副本。

    存下来就多了一个会与另外两个漂开的数，而漂开之后没有任何东西会报错。
    这条断言 `model_dump()` 里没有它。
    """
    recipe = build_recipe(
        profile(median_length=180.0), signals(), config=CFG, hard_max_length=140
    )
    dumped = recipe.model_dump()
    assert "effective_target_length" not in dumped
    assert dumped["observed_target_length"] == 180
    assert dumped["hard_max_length"] == 140


def test_every_choice_carries_a_reason():
    recipe = build_recipe(profile(), signals(), config=CFG, profile_ref="corpus@T")
    # 八个参数里除去中性/无数据的那些，也应当留下多条依据
    assert len(recipe.reasons) >= 5
    assert all(reason.strip() for reason in recipe.reasons)
    assert any("corpus@T" in reason for reason in recipe.reasons)


def test_profile_ref_is_threaded_through():
    """没有可回指的引用，recipe 就是一组无法复核的参数。"""
    recipe = build_recipe(profile(), signals(), config=CFG, profile_ref="corpus@2026-09-30T11:00")
    assert recipe.profile_ref == "corpus@2026-09-30T11:00"


# ---------------------------------------------------------------------------
# 证据决定例外：结构服务于内容
# ---------------------------------------------------------------------------


def test_two_counterpoints_switch_to_the_contrast_structure():
    """反方说法够多时，对比结构在事实上更站得住，即使它在语料里不是最常见的。"""
    recipe = build_recipe(profile(), signals(counterpoint_count=2), config=CFG)
    assert recipe.structure_type is StructureType.CONTRAST


def test_one_counterpoint_is_not_enough_to_override_the_corpus():
    recipe = build_recipe(profile(), signals(counterpoint_count=1), config=CFG)
    assert recipe.structure_type is StructureType.OPINION_FIRST


def test_numeric_fact_switches_the_hook_to_statistic():
    recipe = build_recipe(profile(), signals(has_numeric_fact=True), config=CFG)
    assert recipe.hook_type is HookType.STATISTIC


# ---------------------------------------------------------------------------
# evidence_signals：三个能被数出来的东西
# ---------------------------------------------------------------------------


def test_evidence_signals_of_no_pack_are_all_zero():
    assert evidence_signals(None) == EvidenceSignals()


def test_evidence_signals_counts_facts_and_counterpoints():
    pack = EvidencePack(
        facts=[EvidenceFact(fact_id="f1", fact="成本降了"), EvidenceFact(fact_id="f2", fact="用量涨了")],
        counterpoints=["有人说这不成立"],
        unknowns=["缺官方数据"],
    )
    result = evidence_signals(pack)
    assert result.fact_count == 2
    assert result.counterpoint_count == 1
    assert result.unknown_count == 1


def test_a_fact_without_digits_is_not_numeric():
    """**回归测试**：`has_numeric_fact` 只看事实正文。

    早期实现用 `getattr(fact, "statement", None) or str(fact)` 兜底，而
    `EvidenceFact` 的字段名是 `fact`——于是落到 `str(fact)`，把模型 repr 里的
    `fact_id='f1'` 也算进来，那个 `1` 让"有数字"对**每一条**事实都为真。
    后果是每轮都选数据型开头，而依据是一个 id 里的序号。
    """
    pack = EvidencePack(facts=[EvidenceFact(fact_id="f1", fact="成本在下降，仅此而已")])
    assert evidence_signals(pack).has_numeric_fact is False


def test_a_fact_with_digits_is_numeric():
    pack = EvidencePack(facts=[EvidenceFact(fact_id="f1", fact="推理成本降了 90%")])
    assert evidence_signals(pack).has_numeric_fact is True


def test_only_the_first_numeric_fact_is_needed():
    """只要有一条带数字的事实就够——它是个布尔信号，不是计数。"""
    pack = EvidencePack(
        facts=[
            EvidenceFact(fact_id="f1", fact="没有数字"),
            EvidenceFact(fact_id="f2", fact="有 3 个数字 2024 年 90%"),
        ]
    )
    assert evidence_signals(pack).has_numeric_fact is True


# ---------------------------------------------------------------------------
# 分档
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected",
    [(0, StanceStrength.SOFT), (1, StanceStrength.MODERATE), (2, StanceStrength.STRONG)],
)
def test_stance_ladder(count, expected):
    assert build_recipe(profile(), signals(counterpoint_count=count), config=CFG).stance_strength is expected


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, EvidenceRequirement.OPTIONAL),
        (1, EvidenceRequirement.REQUIRED),
        (4, EvidenceRequirement.STRICT),
    ],
)
def test_evidence_requirement_ladder(count, expected):
    recipe = build_recipe(profile(), signals(fact_count=count), config=CFG)
    assert recipe.evidence_requirement is expected


@pytest.mark.parametrize(
    "density,expected",
    [(0.5, DensityLevel.LOW), (2.0, DensityLevel.MEDIUM), (5.0, DensityLevel.HIGH)],
)
def test_density_ladder(density, expected):
    recipe = build_recipe(profile(number_density=density), signals(fact_count=3), config=CFG)
    assert recipe.density is expected


def test_high_density_is_refused_when_there_is_nothing_to_be_dense_about():
    """没有事实却要求高密度，只会得到车轱辘话。

    这是"结构约束不能凭空要求内容"那条边界的落点：约束可以要求怎么表达，
    不能要求表达不存在的东西。
    """
    recipe = build_recipe(
        profile(number_density=9.0), signals(fact_count=0, counterpoint_count=0), config=CFG
    )
    assert recipe.density is DensityLevel.MEDIUM
    assert any("空手写不出高密度" in reason for reason in recipe.reasons)


def test_missing_density_measure_falls_back_to_medium():
    recipe = build_recipe(profile(number_density=None), signals(), config=CFG)
    assert recipe.density is DensityLevel.MEDIUM


@pytest.mark.parametrize(
    "closing,expected",
    [
        ({"question": 1.0}, EndingType.QUESTION),
        ({"call": 1.0}, EndingType.CALL),
        ({"statement": 1.0}, EndingType.CONCLUSION),
    ],
)
def test_ending_follows_the_corpus(closing, expected):
    recipe = build_recipe(profile(closing_distribution=closing), signals(), config=CFG)
    assert recipe.ending_type is expected


def test_ending_becomes_an_observation_when_there_is_no_evidence():
    """证据不足时只做观察陈述，不强行下结论。"""
    recipe = build_recipe(
        profile(closing_distribution={"statement": 1.0}),
        signals(fact_count=0, counterpoint_count=0),
        config=CFG,
    )
    assert recipe.ending_type is EndingType.OBSERVATION


def test_emoji_heavy_corpus_reads_as_conversational():
    recipe = build_recipe(profile(emoji_density=2.0), signals(), config=CFG)
    assert recipe.tone is Tone.CONVERSATIONAL


def test_data_reading_corpus_reads_as_analytical():
    recipe = build_recipe(
        profile(structure_distribution={StructureType.DATA_READING.value: 1.0}),
        signals(),
        config=CFG,
    )
    assert recipe.tone is Tone.ANALYTICAL
