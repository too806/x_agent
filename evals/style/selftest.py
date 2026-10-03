"""Style Benchmark 的自检：把四条承诺变成可执行的断言。

跑法：

    python -m evals.style.selftest

与 `evals/selftest.py` 同一套纪律——断言的方向是"**违规数据必须失败**"，
而不是"干净数据应当通过"。一个只会说 OK 的基准没有价值：它最危险的失效
方式是静默地永远通过，那样"通过"与"没跑起来"长得一模一样。

所以这里每一条检查都尽量配一个反例：
- Profile 的确定性，用"把语料顺序打乱"来反证；
- "prompt 里没有样本原文"，用"故意把一条样本塞进去"来反证；
- 筛选规则，用一批必须被拒的候选来反证。

全程离线：不碰 X、不碰 LLM、不读数据库、不取挂钟时间。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from evals._bootstrap import force_utf8_stdout

from evals.style import corpus
from evals.style.spans import SHARED_SPAN, corpus_spans, overlapping_spans

force_utf8_stdout()

from app.agent.prompts import format_style_recipe, load_prompt, render  # noqa: E402
from app.style.config import load_style_config  # noqa: E402
from app.style.corpus import CandidateSample, evaluate_sample, filter_candidates  # noqa: E402
from app.style.profile import compute_profile  # noqa: E402
from app.style.recipe import EvidenceSignals, build_recipe  # noqa: E402
from app.style.schemas import (  # noqa: E402
    DensityLevel,
    EndingType,
    EvidenceRequirement,
    StanceStrength,
    StyleRecipe,
    Tone,
)
from app.style.taxonomy import (  # noqa: E402
    HookType,
    StructureType,
    coerce_label,
)

CFG = load_style_config()


class StyleSelfTestFailure(AssertionError):
    """一条检查失败。与 `evals/selftest.py::SelfTestFailure` 同一角色。"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise StyleSelfTestFailure(message)


def _profile():
    return compute_profile(corpus.SAMPLES)


#: benchmark 用的目标长度上限。取 140 与 `config/persona.yaml` 同值——
#: 这样"语料目标被压过"这条路径在离线基准里真的会被走到，
#: 而不会因为基准里恰好没设上限而永远测不到。
#:
#: 名字里的 "hard" 指的是"基准里把一个硬顶压到 recipe 的目标上"，不再是"线上发
#: 不出去"。140 与 config 同值的理由仍然成立（同一套压顶逻辑要能在基准里跑到）；
#: 但"140 是平台的硬限制"这个说法已经过期——账号是 Premium，发布前的超长 422
#: 也撤了（见 app/services/publish.py 的 `_preflight`）。这里压的是写作目标。
BENCH_HARD_MAX_LENGTH = 140


def _recipe(
    profile=None,
    signals=None,
    *,
    profile_ref="corpus@benchmark",
    hard_max_length: int | None = BENCH_HARD_MAX_LENGTH,
):
    return build_recipe(
        profile if profile is not None else _profile(),
        signals or EvidenceSignals(fact_count=3, counterpoint_count=1, has_numeric_fact=True),
        config=CFG,
        profile_ref=profile_ref,
        hard_max_length=hard_max_length,
    )


def _corpus_spans(n: int = SHARED_SPAN) -> set[str]:
    """合成基准语料的全部 n-gram。检测器本体在 `app/style/spans.py`。"""
    return corpus_spans([sample.content for sample in corpus.SAMPLES], n)


def _overlapping_spans(text: str, n: int = SHARED_SPAN) -> list[str]:
    """`text` 里与**基准语料**共享的片段。空列表 = 没有搬运任何原文。"""
    return overlapping_spans(text, _corpus_spans(n), n)


# ---------------------------------------------------------------------------
# 一、语料完整性
# ---------------------------------------------------------------------------


def check_corpus_is_synthetic_and_complete() -> list[str]:
    """语料必须是合成的、非空的、长度合法的。

    工作单第四十节禁止复制真实帖子的原文。一份 benchmark 只要装进真实内容，
    后来的人就会把它当"参考语料"复制走——而它在这里的唯一身份是测试数据。
    所以这两件事一起查：不得有外链（可能是抄来的出处），不得有 @handle
    （可能是真实的账号）。
    """
    _check(len(corpus.SAMPLES) >= 8, "语料样本少于 8 条，分布不足以比较")
    for sample in corpus.SAMPLES:
        _check(sample.content.strip(), f"{sample.post_id} 的正文为空")
        _check(
            len(sample.content) >= CFG.corpus.min_length,
            f"{sample.post_id} 短于 min_length，它本该被筛选规则拒掉",
        )
        _check(
            len(sample.content) <= CFG.corpus.max_length,
            f"{sample.post_id} 长于 max_length",
        )
        _check("http" not in sample.content, f"{sample.post_id} 里出现了链接")
        _check("@" not in sample.content, f"{sample.post_id} 里出现了 handle")
    _check(corpus.CORPUS_NOTE.strip(), "语料必须带上「这是合成数据」的说明")
    return [f"{len(corpus.SAMPLES)} 条合成样本，无外链、无 handle、长度合法"]


def check_sample_has_required_fields() -> list[str]:
    """必填字段齐全，且 id 唯一。

    id 重复会让"这条分布基于几条样本"失去答案——两条不同的样本在统计里
    变成同一条。
    """
    ids = [sample.post_id for sample in corpus.SAMPLES]
    _check(len(ids) == len(set(ids)), f"post_id 有重复：{ids}")
    for sample in corpus.SAMPLES:
        _check(sample.structure_type, f"{sample.post_id} 缺少 structure_type")
        _check(sample.hook_type, f"{sample.post_id} 缺少 hook_type")
    return [f"{len(ids)} 条样本的必填字段齐全，id 唯一"]


def check_taxonomy_labels_are_valid() -> list[str]:
    """每条的标注都能收进分类法。

    标错一个字母（`opinion-first`）会让它静默落进 `unclassified`——
    于是 Profile 的分布里少了一个类别，而报告看不出任何异常。
    所以这里**不接受** unclassified：语料是我们自己写的，它必须归得进去。
    """
    structures = {sample.structure_type for sample in corpus.SAMPLES}
    hooks = {sample.hook_type for sample in corpus.SAMPLES}
    for value in structures:
        _check(
            coerce_label(StructureType, value) == value,
            f"{value!r} 不是有效的 structure_type",
        )
    for value in hooks:
        _check(coerce_label(HookType, value) == value, f"{value!r} 不是有效的 hook_type")
    return [f"结构覆盖 {len(structures)} 类，开头覆盖 {len(hooks)} 类，全部合法"]


def check_the_corpus_covers_more_than_one_label() -> list[str]:
    """语料必须在结构和 hook 上都**不止一类**。

    只有一个类别时，"取分布里最常见的那一项"这条逻辑无论对错都会通过——
    那正是最需要被检验的逻辑。
    """
    profile = _profile()
    _check(
        len(profile.structure_distribution) >= 3,
        f"结构分布只有 {len(profile.structure_distribution)} 类，覆盖不足",
    )
    _check(
        len(profile.hook_distribution) >= 3,
        f"开头分布只有 {len(profile.hook_distribution)} 类，覆盖不足",
    )
    return [
        f"结构 {len(profile.structure_distribution)} 类，开头 {len(profile.hook_distribution)} 类"
    ]


# ---------------------------------------------------------------------------
# 二、筛选规则：反例集必须全部被拒
# ---------------------------------------------------------------------------


def check_filter_rejects_every_bad_candidate() -> list[str]:
    """每一条反例都必须被拒，且理由正是它对应的那一条。

    只查"被拒了"不够：`evaluate_sample` 是短路的，顺序写错会让一条纯链接
    的帖子因为"太短"被拒——结论对了，理由错了，而理由正是采集规模不足时
    唯一能用来解释的东西。
    """
    rejected = []
    for candidate, expected_reason in corpus.REJECTED:
        decision = evaluate_sample(candidate, CFG.corpus)
        _check(
            not decision.included,
            f"{candidate.platform_post_id} 本该被拒，却被收进了语料",
        )
        _check(
            (decision.reason or "").startswith(expected_reason),
            f"{candidate.platform_post_id} 的拒绝理由应为 {expected_reason}，"
            f"实际是 {decision.reason!r}",
        )
        rejected.append(candidate.platform_post_id)
    return [f"{len(rejected)} 条反例全部被拒，且理由逐条对得上"]


def check_a_clean_candidate_is_included() -> list[str]:
    """反例全被拒还不够——一个恒拒的筛选器也能通过上一条。

    所以补一条正向的：语料里的一条样本原样拿去过筛，必须被收下。
    """
    for sample in corpus.SAMPLES:
        candidate = CandidateSample(
            platform_post_id=f"clean-{sample.post_id}",
            author_username="synthetic",
            content=sample.content,
            likes=50,
            reposts=20,
        )
        decision = evaluate_sample(candidate, CFG.corpus)
        _check(decision.included, f"{sample.post_id} 这条干净样本被拒了：{decision.reason}")
    return [f"{len(corpus.SAMPLES)} 条干净样本全部通过筛选"]


def check_filtering_is_reproducible() -> list[str]:
    """同一批候选跑两遍，结论与理由逐条相同。

    筛选读的是配置里的阈值。如果哪天有人在筛选路径里引入了时间或随机，
    这条会立刻红——而它红了意味着"昨天收进来的样本今天收不进来"。
    """
    candidates = [candidate for candidate, _ in corpus.REJECTED]
    first = [(d.included, d.reason) for _, d in filter_candidates(candidates, CFG.corpus)]
    second = [(d.included, d.reason) for _, d in filter_candidates(candidates, CFG.corpus)]
    _check(first == second, "同一批候选两次筛选结果不同")
    return [f"{len(candidates)} 条候选的筛选结论可复现"]


# ---------------------------------------------------------------------------
# 三、Profile 的确定性（Smoke 5）
# ---------------------------------------------------------------------------


def check_profile_is_bit_identical_across_runs() -> list[str]:
    """**同一份 corpus → 同一个 Profile**，逐字段相同。

    这是整条"从真实统计里提炼结构"链路的前提：一个会漂的 Profile 无法被
    引用、无法被比较，也无法解释——因为"为什么选这个结构"的答案会变成
    "上次跑的时候它是这样"。
    """
    first = _profile().model_dump()
    second = _profile().model_dump()
    _check(first == second, "同一份语料两次算出的 Profile 不一致")
    return ["Profile 逐字段可复现"]


def check_profile_is_order_independent() -> list[str]:
    """打乱样本顺序，Profile 必须不变。

    这条挡的是"第一条样本决定了什么"这类 bug。真实采集回来的顺序每次都不
    一样，结果若跟着变，Profile 就不可比较了。
    """
    forward = compute_profile(corpus.SAMPLES).model_dump()
    backward = compute_profile(tuple(reversed(corpus.SAMPLES))).model_dump()
    _check(forward == backward, "样本顺序影响了 Profile：结果不可复现")
    return ["正序与逆序样本得到同一个 Profile"]


def check_profile_accounts_for_every_sample() -> list[str]:
    """分布的分母必须等于样本数。

    丢掉未标注的样本会让"这条分布基于几条样本"失去答案——而落进
    `unclassified` 是诚实的：这个类别就是"没归进去"。
    """
    profile = _profile()
    _check(
        profile.sample_count == len(corpus.SAMPLES),
        f"sample_count={profile.sample_count}，实际样本 {len(corpus.SAMPLES)} 条",
    )
    for name in ("structure_distribution", "hook_distribution", "opening_distribution"):
        distribution = getattr(profile, name)
        _check(
            abs(sum(distribution.values()) - 1.0) < 1e-9,
            f"{name} 的占比之和是 {sum(distribution.values())}，不是 1",
        )
    return [f"{profile.sample_count} 条样本全部计入分布"]


def check_profile_reports_unknowns_as_none() -> list[str]:
    """算不出来的量必须是 None，不能是 0.0。

    `0` 会被读成"测出来是零"，而"没测"与"测出来是零"在聚合时差别是全部。
    空语料是唯一能稳定构造"算不出来"的输入。
    """
    empty = compute_profile([])
    _check(empty.sample_count == 0, "空语料应当得到 sample_count=0")
    for name in ("median_length", "p25_length", "emoji_density", "number_density"):
        _check(getattr(empty, name) is None, f"空语料的 {name} 应当是 None，不是 0.0")
    _check(empty.structure_distribution == {}, "空语料的分布应当是空 dict")
    return ["空语料给出 None 而不是 0.0"]


# ---------------------------------------------------------------------------
# 四、Recipe 是结构化的约束
# ---------------------------------------------------------------------------


def check_recipe_is_structured() -> list[str]:
    """recipe 的每个字段都必须是有限取值里的一个，长度落在配置区间内。

    这条是"recipe 是约束，不是正文"的类型层断言：一旦有人加一个装原文的
    自由文本字段，Writer 就可能开始抄它。
    """
    recipe = _recipe()
    for name, enum_cls in (
        ("structure_type", StructureType),
        ("hook_type", HookType),
        ("density", DensityLevel),
        ("stance_strength", StanceStrength),
        ("evidence_requirement", EvidenceRequirement),
        ("tone", Tone),
        ("ending_type", EndingType),
    ):
        _check(
            isinstance(getattr(recipe, name), enum_cls),
            f"recipe.{name} 不是 {enum_cls.__name__} 的成员",
        )
    observed = recipe.observed_target_length
    _check(isinstance(observed, int), "recipe.observed_target_length 不是整数")
    _check(
        CFG.recipe.min_target_length <= observed <= CFG.recipe.max_target_length,
        f"observed_target_length={observed} 落在配置区间之外",
    )
    _check(recipe.reasons, "recipe 没有任何依据（reasons 为空）")
    _check(recipe.profile_ref, "recipe 没有 profile_ref，事后无法回到那一版 Profile")
    return [f"{len(recipe.reasons)} 条依据，语料目标长度 {observed} 字"]


def check_hard_max_is_separate_from_the_corpus_target() -> list[str]:
    """平台硬上限与语料目标是**两个数**，且收缩必须被解释。

    这条守的是一个具体的误导：语料说 180、平台只让发 140 时，如果两者被
    压成一个字段，Run Detail 上就只剩一个 140。任何人都会把它读成
    "我们学到的写法就是写 140 字"——而语料从没这么说，它被平台的容量
    限制删掉了 40 个字的信息量，那不是同一个结论。
    """
    # 基准语料的中位长度（77 字）落在 `min_target_length` 之下，所以这里
    # 期望的是**收敛到配置区间之后**的值。那个区间是"这个长度值不值得写"
    # 的合理性带，与平台上限无关，两者作用在同一个数的不同阶段。
    raw = int(round(_profile().median_length))
    expected_observed = max(CFG.recipe.min_target_length, min(CFG.recipe.max_target_length, raw))

    # 上限刻意取得比语料目标低，好让"被压过"这条分支真的被走到——
    # 一个触发不了它的基准值等于这条检查什么都没测。
    cap = expected_observed - 20
    capped = _recipe(hard_max_length=cap)
    _check(
        capped.observed_target_length == expected_observed,
        f"observed_target_length 不是语料中位数收敛到配置区间后的值"
        f"（期望 {expected_observed}，得到 {capped.observed_target_length}）",
    )
    _check(
        capped.observed_target_length > capped.hard_max_length,
        "基准没构造出「语料目标大于平台上限」的情形，这条检查什么也没测到",
    )
    _check(
        capped.effective_target_length == capped.hard_max_length,
        "被压过时执行值应当等于硬上限",
    )
    _check(
        any(str(capped.hard_max_length) in reason for reason in capped.reasons),
        "长度被硬上限压过，reasons 里却没有任何一句解释",
    )
    _check(
        capped.observed_target_length is not capped.effective_target_length
        or capped.hard_max_length is None,
        "语料目标被硬上限改写成了执行值——这正是要避免的那种压平",
    )

    # 反方向：上限比语料目标大时**不能**出现收缩说明，也不该改写任何东西。
    roomy = _recipe(hard_max_length=expected_observed + 1000)
    _check(roomy.effective_target_length == roomy.observed_target_length, "上限宽松时不该收缩")
    _check(
        not any("硬上限" in reason for reason in roomy.reasons),
        "上限没起作用时不该解释一次并不存在的收缩",
    )
    return [
        f"语料目标 {capped.observed_target_length} 字 / 硬上限 "
        f"{capped.hard_max_length} 字 / 执行值 {capped.effective_target_length} 字，三者分离"
    ]


def check_a_neutral_recipe_does_not_invent_a_length() -> list[str]:
    """没有语料依据时**不得**拿硬上限冒充目标长度。

    "没有证据"与"目标就是上限"是两个不同的陈述。后者会让 Writer 收到
    一个语料从未支持的长度，而它在报告里看起来与"语料说该写这么长"一模一样。
    """
    neutral = build_recipe(None, hard_max_length=BENCH_HARD_MAX_LENGTH)
    _check(
        neutral.observed_target_length is None,
        "中性 recipe 的 observed_target_length 应当是 None",
    )
    _check(
        neutral.effective_target_length is None,
        "中性 recipe 不得拿硬上限冒充目标长度",
    )
    _check(neutral.is_length_capped is False, "没有语料依据时不该报告「被上限压过」")
    return ["无语料依据时长度为 None，硬上限没有被冒充成目标"]


def check_the_recipe_never_exceeds_the_hard_max() -> list[str]:
    """任何上限下，**执行**长度都不得超过它；且**语料目标**不得被它改写。

    这条是工作单第十二节"recipe 必须尊重硬上限"的机器版本。它与上一条查的是
    两件事：上一条问"被压过时有没有解释清楚"，这一条问"压过之后执行值到底
    有没有越界"。

    上限设得很小时（40 字）也要成立——`min_target_length` 是 80，所以这是一次
    **配置区间与平台上限正面对撞**的情形。取小值必须发生在收敛之后，
    否则"平台让发 40 字"会被读成"语料说该写 40 字"，而那低于配置里
    "还值得写"的下界。
    """
    base = _recipe()
    corpus_target = base.observed_target_length
    for cap in (40, 79, 100, 139, 140, 155, 200, 1000):
        recipe = _recipe(hard_max_length=cap)
        effective = recipe.effective_target_length
        _check(
            effective is None or effective <= cap,
            f"硬上限 {cap} 字时执行目标是 {effective} 字——越过了硬上限",
        )
        _check(
            recipe.observed_target_length == corpus_target,
            f"硬上限 {cap} 字改写了语料目标"
            f"（{corpus_target} → {recipe.observed_target_length}）",
        )
        _check(
            recipe.hard_max_length == cap,
            f"recipe 没有如实记下传入的硬上限（期望 {cap}，得到 {recipe.hard_max_length}）",
        )
    return [f"硬上限 40～1000 字八档下执行目标均不越界，语料目标始终是 {corpus_target} 字"]


def check_recipe_is_deterministic() -> list[str]:
    """同一 Profile + 同一证据信号 → 同一个 recipe。

    含并列的情形：结构分布五五开时取键序最小的那一项。按插入顺序选会让
    同一份 Profile 在不同进程里给出不同的结构，而原因看不见。
    """
    a = _recipe().model_dump()
    b = _recipe().model_dump()
    _check(a == b, "同一输入两次得到的 recipe 不一致")

    tie = {StructureType.OPINION_FIRST.value: 0.5, StructureType.CONTRAST.value: 0.5}
    from app.style.schemas import StyleProfileFeatures

    tie_profile = StyleProfileFeatures(
        sample_count=10, median_length=200.0, structure_distribution=tie
    )
    first = build_recipe(tie_profile, EvidenceSignals(), config=CFG)
    reversed_tie = StyleProfileFeatures(
        sample_count=10,
        median_length=200.0,
        structure_distribution=dict(reversed(list(tie.items()))),
    )
    second = build_recipe(reversed_tie, EvidenceSignals(), config=CFG)
    _check(
        first.structure_type is second.structure_type,
        "并列的分布因字典顺序给出了不同的结构",
    )
    return ["recipe 可复现，并列取舍与字典顺序无关"]


def check_recipe_falls_back_to_neutral_without_a_profile() -> list[str]:
    """没有 Profile 时返回中性 recipe，而不是 None。

    两条代码路径（有/无 recipe）是 Smoke 4 要避免的东西：让"没有 Profile"
    在 recipe 内部表达，调用方就只有一条路径。
    """
    recipe = build_recipe(None)
    _check(isinstance(recipe, StyleRecipe), "无 Profile 时应当仍返回一个 StyleRecipe")
    _check(recipe.structure_type is StructureType.UNCLASSIFIED, "中性 recipe 不该指定结构")
    _check(recipe.observed_target_length is None, "中性 recipe 不该指定长度")
    _check(recipe.reasons, "中性 recipe 必须说明为什么没有约束")
    return ["无 Profile 时给出中性 recipe 并写明原因"]


def check_recipe_never_carries_sample_text() -> list[str]:
    """**recipe 的每个字符串字段都不得含有语料原文。**

    这是工作单第二十二、四十节的机器守卫：Writer 收到的是约束，不是样本。
    查的是 recipe 对象本身（含 `reasons` 与 `profile_ref`），而不是渲染后的
    文本——后者会把"recipe 里混进了原文"与"渲染时混进了原文"混为一谈。
    """
    recipe = _recipe()
    for name, value in recipe.model_dump().items():
        for text in value if isinstance(value, list) else [value]:
            if not isinstance(text, str):
                continue
            shared = _overlapping_spans(text)
            _check(
                not shared,
                f"recipe.{name} 里出现了语料原文片段：{shared[:3]}",
            )
    return [f"recipe 的字符串字段与语料无超过 {SHARED_SPAN} 字的公共片段"]


def check_writer_prompt_has_no_sample_text() -> list[str]:
    """**渲染出来的 Writer prompt 里不得含有语料原文。**

    上面那条查的是 recipe 对象；这一条查的是真正发出去的那段文本——
    包括模板本身的静态部分与渲染后的 `{{STYLE}}`。两者都需要：只查对象
    会漏掉"模板里被塞进了样本"，只查文本会说不清是对象还是渲染出的问题。

    反证见 `check_the_shared_span_detector_actually_fires`。
    """
    prompt = _render_writer_prompt(_recipe())
    shared = _overlapping_spans(prompt)
    _check(not shared, f"Writer prompt 里出现了语料原文片段：{shared[:3]}")
    return [f"Writer prompt 与语料无超过 {SHARED_SPAN} 字的公共片段"]


def check_the_shared_span_detector_actually_fires() -> list[str]:
    """**反证**：把一条样本原文塞进 prompt，检测器必须报警。

    没有这条，上面两条检查的"通过"可能只是因为检测器永远返回空集——
    一个恒真的守卫比没有守卫更糟，因为它会让人以为这条禁止被守住了。
    """
    poisoned = _render_writer_prompt(_recipe()) + "\n" + corpus.SAMPLES[0].content
    shared = _overlapping_spans(poisoned)
    _check(
        shared,
        f"把样本原文接进 prompt 后仍检测不到共享片段（阈值 {SHARED_SPAN} 失效）",
    )
    return [f"塞入样本原文后检测到 {len(shared)} 个共享片段"]


def check_writer_prompt_is_stable_for_the_same_recipe() -> list[str]:
    """同一个 recipe → 同一段 prompt。

    这条保证"换了 recipe 才换写法"，而不是"每次渲染都不同"——后者会让
    Smoke 2/3 的对比失去意义（比出来的差异可能只是渲染噪声）。
    """
    first = _render_writer_prompt(_recipe())
    second = _render_writer_prompt(_recipe())
    _check(first == second, "同一个 recipe 两次渲染出的 prompt 不同")
    return ["同一 recipe 的 Writer prompt 逐字可复现"]


def check_style_block_is_present_in_the_writer_prompt() -> list[str]:
    """`{{STYLE}}` 必须真的被替换掉了。

    `render()` 只替换传进去的键，对没传的占位符**不报错**——它会把字面量
    `{{STYLE}}` 原样发给模型。漏传的后果不是崩溃，是安静地少了一整段输入。
    """
    prompt = _render_writer_prompt(_recipe())
    _check("{{" not in prompt, "渲染后的 prompt 里残留了未替换的占位符")
    _check("选择依据：" in prompt, "prompt 里没有 recipe 的选择依据")
    return ["{{STYLE}} 已被替换，prompt 里无残留占位符"]


# ---------------------------------------------------------------------------
# 渲染 Writer prompt
# ---------------------------------------------------------------------------


def _render_writer_prompt(recipe: StyleRecipe) -> str:
    """按 candidates.txt 的**真实模板**渲染一次 Writer prompt。

    其余占位符一律填短而独特的合成值：它们既不该撞上语料原文（会让
    `check_writer_prompt_has_no_sample_text` 误报），也不该为空
    （空值会让"模板里有没有样本"这件事无从判断）。
    """
    template = load_prompt("candidates")
    names = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", template)))
    filler = {
        name: f"<{name} 的基准占位值>"
        for name in names
        if name != "STYLE"
    }
    filler["STYLE"] = format_style_recipe(recipe)
    return render(template, **filler)


# ---------------------------------------------------------------------------
# 注册与运行
# ---------------------------------------------------------------------------


CHECKS: tuple[tuple[str, Any], ...] = (
    ("语料是合成的、完整的、长度合法", check_corpus_is_synthetic_and_complete),
    ("每条样本必填字段齐全且 id 唯一", check_sample_has_required_fields),
    ("分类法标签全部合法", check_taxonomy_labels_are_valid),
    ("语料覆盖不止一个类别", check_the_corpus_covers_more_than_one_label),
    ("反例集全部被拒且理由对得上", check_filter_rejects_every_bad_candidate),
    ("干净样本全部通过筛选", check_a_clean_candidate_is_included),
    ("筛选结论可复现", check_filtering_is_reproducible),
    ("同一语料 → 同一 Profile（Smoke 5）", check_profile_is_bit_identical_across_runs),
    ("Profile 与样本顺序无关", check_profile_is_order_independent),
    ("分布的分母等于样本数", check_profile_accounts_for_every_sample),
    ("算不出来的量是 None 而不是 0", check_profile_reports_unknowns_as_none),
    ("recipe 是结构化的约束", check_recipe_is_structured),
    ("语料目标与平台硬上限是两个数", check_hard_max_is_separate_from_the_corpus_target),
    ("无语料依据时不拿硬上限冒充长度", check_a_neutral_recipe_does_not_invent_a_length),
    ("执行长度永不越过硬上限", check_the_recipe_never_exceeds_the_hard_max),
    ("recipe 可复现（含并列取舍）", check_recipe_is_deterministic),
    ("无 Profile 时退回中性 recipe", check_recipe_falls_back_to_neutral_without_a_profile),
    ("recipe 不含语料原文", check_recipe_never_carries_sample_text),
    ("Writer prompt 不含语料原文", check_writer_prompt_has_no_sample_text),
    ("共享片段检测器确实会报警（反证）", check_the_shared_span_detector_actually_fires),
    ("同一 recipe → 同一段 prompt", check_writer_prompt_is_stable_for_the_same_recipe),
    ("{{STYLE}} 已被替换", check_style_block_is_present_in_the_writer_prompt),
)


def run_selftest(*, verbose: bool = False, as_json: bool = False) -> int:
    """跑全部检查。返回进程退出码：0 = 全过。"""
    results: list[dict[str, Any]] = []
    failures = 0
    for name, check in CHECKS:
        try:
            notes = check()
            results.append({"check": name, "passed": True, "notes": notes})
            if not as_json:
                print(f"✓ {name}")
                if verbose:
                    for note in notes:
                        print(f"    {note}")
        except Exception as exc:  # noqa: BLE001 - 一条检查的失败不该中断其余检查
            failures += 1
            results.append(
                {"check": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
            )
            if not as_json:
                print(f"✗ {name}")
                print(f"    {type(exc).__name__}: {exc}")

    if as_json:
        print(json.dumps({"checks": results, "passed": len(CHECKS) - failures,
                          "failed": failures}, ensure_ascii=False, indent=2))
    else:
        print(f"\nStyle 自检：{len(CHECKS) - failures} 通过 / {failures} 失败（共 {len(CHECKS)} 项）")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/style/selftest.py",
        description="Style & Structure 离线基准的自检（不碰 X、不碰 LLM）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条检查的说明")
    args = parser.parse_args(argv)
    return run_selftest(verbose=args.verbose, as_json=args.json)


if __name__ == "__main__":  # pragma: no cover - 入口
    sys.exit(main())
