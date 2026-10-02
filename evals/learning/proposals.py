"""学习提案的离线基准（Phase 5D 工作单第二十六节）。

跑法：

    python -m evals.learning.proposals

八类是工作单点名的那八类，一类不多一类不少：

    valid_learning_evidence   5+5 干净数据 → 产出一条提案
    insufficient_sample       n=1 → 闸门关，**零条**
    missing_metric            三类计数都稀疏 → 闸门关，**零条**
    mixed_window              两半记录窗口不同 → 闸门关，**零条**
    causal_evidence           差异明显的数据 → 措辞仍然不含因果词
    invalid_target            目标不在允许范围内 → invalid
    security_target           目标命中安全/生产边界 → invalid
    proposal_without_evidence 没有 evidence_ids 的提案 → invalid

## 这份基准守的核心只有一句

    **invalid ⇒ proposal = none**

前五类走**生成路径**：检查 `generate_proposals()` 在什么条件下什么都不产出，
以及产出时那句话经不经得起因果检查。后三类走**校验路径**：它们是手写的提案
（生成路径永远产不出这种），用来证明 `validate_proposal()` 真的会把它们拦下来。

两条路径必须分开测，因为它们是两道不同的闸门：

    闸门（learning_ready）        决定"要不要提"
    校验（validate_proposal）     决定"提得对不对"

只测前者，"目标写成 publish"这种提案就永远没人验；只测后者，"小样本也敢提"
这种错误也照样溜过去。

## 反证的方向

同 `selftest.py`：断言的是"**违规数据必须失败**"。每一条"应该有提案"的旁边
都有一条"这种情况下必须一条都没有"，因为一个恒真的守卫比没有守卫更糟——
它让人以为那条禁令被守住了。

全程离线：不碰 X、不碰 LLM、不读数据库、不取挂钟时间。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from evals._bootstrap import force_utf8_stdout

from evals.learning import fixtures  # noqa: E402
from evals.learning.fixtures import WINDOW  # noqa: E402

force_utf8_stdout()

from app.feedback.signals import (  # noqa: E402
    find_causal_language,
    find_instruction_language,
)
from app.learning.proposal import (  # noqa: E402
    ABSOLUTE_MARKERS,
    VALIDATION_CHECKS,
    generate_proposals,
    validate_proposal,
)
from app.learning.report import build_learning_report  # noqa: E402
from app.learning.schema import (  # noqa: E402
    LearningProposal,
    ProposalStatus,
    ProposalValidation,
)
from app.style.recipe import build_recipe, recipe_version  # noqa: E402
from app.style.schemas import StyleProfileFeatures  # noqa: E402

#: 基准配方的取值：开头方式取 **statement**。
#:
#: 这不是"生产上真的用这一版"——它是**基准**，让提案有东西可说：
#: `fixtures.balanced_cohorts` 里 question 一侧更高，于是"当前 statement →
#: 值得尝试 question"是一条真实的变化。基准取值若已经等于更高的一侧，
#: 生成阶段会正确地拒绝产出提案（见 `_build_for_target`），那一类由
#: `check_already_in_effect_produces_nothing` 单独反证。
BASE_HOOK_DISTRIBUTION = {"statement": 0.6, "question": 0.4}

#: 越界目标被拒时，`validate_proposal` 用来点明"碰到安全线"的那句话。
#:
#: 这里**故意**把生产的措辞抄一份：基准要断言的正是"这条理由真的被说出来了"，
#: 而不是"某条理由存在"。代价是生产改措辞时基准会红——所以另配了一条金丝雀
#: （`check_invalid_never_comes_out_of_generation`），改词会以"生产措辞变了"
#: 的名义报错，而不是伪装成"安全边界没被点出来"。
SECURITY_BOUNDARY_PHRASE = "安全或生产边界"

#: 允许出现在预期里的关系词。**封闭表**：出现表外的词直接判失败，
#: 而不是猜一个语义（同 `selftest.py::RELATIONS`）。
RELATIONS = (
    "==",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
    "is",
    "contains",
    "not_contains",
    "empty",
    "nonempty",
    "none_contain",
    "all_contain",
)

#: 一条预期：`(字段, 关系, 期望值)`。**列表而不是 kwargs**：同一个字段上
#: 常常要断言两件事（"全部被拒"**且**"没有一条被说成安全问题"），
#: 用关键字参数会让第二次赋值静默覆盖第一次。
Expectation = tuple[str, str, Any]


def base_recipe():
    """基准 StyleRecipe（`statement` 开头）。

    用真实构造函数而不是手搓一个对象：提案里的版本号要能对上一个**真的**
    会出现在运行期的 recipe，否则"当前版本 / 建议版本"这两个数字就只是两个
    自己编的字符串。
    """
    profile = StyleProfileFeatures(
        sample_count=40,
        median_length=180.0,
        number_density=1.0,
        emoji_density=0.1,
        structure_distribution={"contrast": 0.5, "list": 0.3},
        hook_distribution=dict(BASE_HOOK_DISTRIBUTION),
        opening_distribution={},
        closing_distribution={"statement": 0.7},
        sentence_length_distribution={},
    )
    return build_recipe(profile)


def report_for(data_category: str):
    """一类 fixture 数据 → 学习报告（窗口取那一类自己的窗口）。"""
    case = fixtures.case_for(data_category)
    return build_learning_report(case.records, window=case.window)


def _generated(data_category: str) -> tuple[Any, tuple[LearningProposal, ...]]:
    report = report_for(data_category)
    return report, generate_proposals(report, base_recipe=base_recipe())


def _template(**overrides: Any) -> LearningProposal:
    """一条**合法**的提案，用来做校验路径的底稿。

    底稿必须本身合法：不合法的话，"改了目标之后它 invalid"就无法说明是**目标**
    被拒，而不是底稿别处本来就有问题。`check_the_template_is_valid` 专门守这一条。
    """
    base: dict[str, Any] = dict(
        id="proposal:hook_type@24-72h",
        target="style_recipe.hook_type",
        current_value="statement",
        proposed_value="question",
        rationale=(
            "在 24-72h 观察窗口下，按开头方式分组，question（n=5）的点赞中位数"
            "为 20，statement（n=5）为 8。在本样本中观察到「question」一侧更高。"
            "这是观察到的差异，不构成因果结论；它只说明在下一步里值得尝试把"
            "「开头方式」取 question（当前为 statement）再观察一次。"
        ),
        evidence_ids=("evidence:c1",),
        sample_count=10,
        observation_window=WINDOW,
        comparison_group="hook_type: question vs statement @ 24-72h · attention",
        limitations=("这是一次观察性比较，没有对照组、没有实验设计。",),
        status=ProposalStatus.PROPOSED,
        current_recipe_version=recipe_version(base_recipe()),
        proposed_recipe_version="sr1-000000000000",
        outlier_count=0,
        automatic_application=False,
    )
    base.update(overrides)
    return LearningProposal(**base)


@dataclass(frozen=True)
class ProposalCase:
    """一组提案，以及它必须满足的预期。

    `source` 区分两条路径（`"generated"` / `"handmade"`），因为
    "生成路径绝不产出 invalid"与"校验路径必须拒绝越界"是两条不同的断言。
    """

    category: str
    note: str
    #: 待检查的提案。**空元组是有意义的值**（"这一类就该零条"）。
    proposals: tuple[LearningProposal, ...]
    #: 逐条校验结果，与 `proposals` 一一对应。
    validations: tuple[ProposalValidation, ...]
    #: 闸门结论。生成路径上它来自报告；手写路径上没有报告，恒为 True
    #: （越界与闸门无关——这正是要分开测的原因）。
    ready: bool
    source: str
    expect: list[Expectation] = field(default_factory=list)


def _generated_case(
    category: str,
    note: str,
    *,
    data_category: str | None = None,
    expect: list[Expectation],
) -> ProposalCase:
    report, proposals = _generated(data_category or category)
    return ProposalCase(
        category=category,
        note=note,
        proposals=proposals,
        validations=tuple(validate_proposal(item, report) for item in proposals),
        ready=report.readiness.learning_ready,
        source="generated",
        expect=list(expect),
    )


def _handmade_case(
    category: str,
    note: str,
    proposals: tuple[LearningProposal, ...],
    *,
    expect: list[Expectation],
) -> ProposalCase:
    return ProposalCase(
        category=category,
        note=note,
        proposals=proposals,
        validations=tuple(validate_proposal(item) for item in proposals),
        ready=True,
        source="handmade",
        expect=list(expect),
    )


def _valid_learning_evidence() -> ProposalCase:
    return _generated_case(
        "valid_learning_evidence",
        "5+5 干净数据、基准配方在另一侧：产出一条可尝试的提案",
        data_category="balanced_cohorts",
        expect=[
            ("ready", "is", True),
            ("proposal_count", "==", 1),
            ("valid_count", "==", 1),
            ("targets", "all_contain", "style_recipe.hook_type"),
            # 三条指标各有一条证据，所以是 3 条；帖子数取**并集**是 10 条，不是 30。
            ("evidence_id_count", "==", 3),
            ("sample_count", "==", 10),
            ("sample_count_is_the_union", "is", True),
            ("confidence_is_always_absent", "is", True),
            ("automatic_application", "is", False),
            ("causal_markers", "empty", None),
            ("instruction_markers", "empty", None),
            # 两个版本号必须**不同**：基准在 statement，建议是 question。
            # 相同的话说明版本指纹没跟着决策字段变，那个数字就没有意义。
            ("versions_differ", "is", True),
            ("passed_checks", "contains", "has_evidence"),
            ("passed_checks", "contains", "automatic_application_false"),
        ],
    )


def _insufficient_sample() -> ProposalCase:
    return _generated_case(
        "insufficient_sample",
        "1 条帖 / 2 条观察：闸门关着，**一条提案都不产出**",
        data_category="one_post",
        expect=[
            ("ready", "is", False),
            ("proposal_count", "==", 0),
            ("valid_count", "==", 0),
            ("gate_forced_zero", "is", True),
        ],
    )


def _missing_metric() -> ProposalCase:
    return _generated_case(
        "missing_metric",
        "三类计数都稀疏：读不到就不补 0，也不产出提案",
        data_category="missing_metrics",
        expect=[
            ("ready", "is", False),
            ("proposal_count", "==", 0),
            ("valid_count", "==", 0),
            ("gate_forced_zero", "is", True),
        ],
    )


def _mixed_window() -> ProposalCase:
    return _generated_case(
        "mixed_window",
        "两半记录的观察窗口不同：不放在一起比，也不产出提案",
        data_category="different_windows",
        expect=[
            ("ready", "is", False),
            ("proposal_count", "==", 0),
            ("valid_count", "==", 0),
            ("gate_forced_zero", "is", True),
        ],
    )


def _causal_evidence() -> ProposalCase:
    return _generated_case(
        "causal_evidence",
        "差异明显（30/15/9 对 5/2/1）：提案照出，但措辞里没有一个因果词",
        data_category="causal_language",
        expect=[
            ("ready", "is", True),
            ("proposal_count", "==", 1),
            ("valid_count", "==", 1),
            ("causal_markers", "empty", None),
            ("instruction_markers", "empty", None),
            ("all_rationales", "all_contain", "不构成因果"),
            # 第二十四节的契约由三张**封闭标记表**守着，不是靠这里再猜一遍
            # 关键词：这里的 `none_contain "更好"` 曾经把
            # "不能说明哪一种写法更好"这句**免责声明**判成了违规——子串匹配
            # 分不清一句断言和它的否定。所以改成断言那三条判据真的过了。
            ("passed_checks", "contains", "not_causal"),
            ("passed_checks", "contains", "not_absolute"),
            ("passed_checks", "contains", "not_a_recommendation"),
            ("absolute_markers", "empty", None),
        ],
    )


def _invalid_target() -> ProposalCase:
    """目标既不在允许范围内，也不命中安全标记——**只**因为越界被拒。"""
    return _handmade_case(
        "invalid_target",
        "目标写成 style_recipe.density / ending_type：不在允许范围内，invalid",
        proposals=(
            _template(target="style_recipe.density"),
            _template(id="proposal:ending_type@24-72h", target="style_recipe.ending_type"),
        ),
        expect=[
            ("valid_count", "==", 0),
            ("invalid_reasons", "all_contain", "不在允许提案的范围内"),
            # 它**不该**被说成安全问题：把"越界"与"碰到安全线"混成一句话，
            # 会让人以为密度这个目标有什么安全隐患。
            ("security_flagged_count", "==", 0),
        ],
    )


def _security_target() -> ProposalCase:
    """目标命中 Trend 权重 / Guardrail / 预算 / Critic 阈值：**永远**不成立。"""
    return _handmade_case(
        "security_target",
        "目标写成 publish / guardrail / budget / critic.threshold：命中安全与生产边界",
        proposals=tuple(
            _template(id=f"proposal:{name}@24-72h", target=name)
            for name in (
                "publish.schedule",
                "guardrail.max_scope_exceeds_evidence",
                "budget.daily_limit",
                "critic.threshold",
                "trend.weight_recency",
            )
        ),
        expect=[
            ("valid_count", "==", 0),
            # 五个目标**每一个**都得被点名为安全/生产边界，而不是"总共提到了"。
            ("security_flagged_count", "==", 5),
        ],
    )


def _proposal_without_evidence() -> ProposalCase:
    """没有 evidence_ids 的提案**直接不成立**：本层没有"凭印象"的建议。"""
    return _handmade_case(
        "proposal_without_evidence",
        "evidence_ids 为空：没有依据的建议一律不成立",
        proposals=(_template(evidence_ids=()),),
        expect=[
            ("valid_count", "==", 0),
            ("invalid_reasons", "all_contain", "没有引用任何 LearningEvidence"),
            ("passed_checks", "not_contains", "evidence_is_a_comparison"),
        ],
    )


#: 八类，顺序固定（工作单第二十六节点名的那八类）。
PROPOSAL_CASES: tuple[ProposalCase, ...] = (
    _valid_learning_evidence(),
    _insufficient_sample(),
    _missing_metric(),
    _mixed_window(),
    _causal_evidence(),
    _invalid_target(),
    _security_target(),
    _proposal_without_evidence(),
)

#: 类别名，顺序与 `PROPOSAL_CASES` 一致。`selftest.py` 用它核对"八类都在"。
PROPOSAL_CATEGORIES: tuple[str, ...] = (
    "valid_learning_evidence",
    "insufficient_sample",
    "missing_metric",
    "mixed_window",
    "causal_evidence",
    "invalid_target",
    "security_target",
    "proposal_without_evidence",
)

#: 闸门关着的那三类：数据本身不足以产出提案，所以只能证明"零条"，
#: 证明不了"闸门是零条的**原因**"。闸门的反证在
#: `check_the_gate_is_load_bearing` 里（用一份本来就能产出提案的数据）。
GATE_CLOSED_CATEGORIES = {
    "insufficient_sample": "one_post",
    "missing_metric": "missing_metrics",
    "mixed_window": "different_windows",
}


def case_for(category: str) -> ProposalCase:
    for case in PROPOSAL_CASES:
        if case.category == category:
            return case
    raise KeyError(category)


# ---------------------------------------------------------------------------
# 预期的求值
# ---------------------------------------------------------------------------

#: 一类的提案集合能被断言的全部字段。**封闭表**：拼错字段名要在这里报错，
#: 而不是抛一个看不出所以然的 AttributeError（同 `selftest.py::REPORT_FIELDS`）。
PROPOSAL_FIELDS = (
    "ready",
    "proposal_count",
    "valid_count",
    "invalid_reasons",
    "security_flagged_count",
    "passed_checks",
    "targets",
    "evidence_ids",
    "evidence_id_count",
    "sample_count",
    "sample_count_is_the_union",
    "rationale",
    "all_rationales",
    "causal_markers",
    "instruction_markers",
    "absolute_markers",
    "confidence_is_always_absent",
    "automatic_application",
    "current_recipe_versions",
    "proposed_recipe_versions",
    "versions_differ",
    "gate_forced_zero",
)


def _resolve(case: ProposalCase, name: str) -> Any:
    if name not in PROPOSAL_FIELDS:
        raise KeyError(f"未知字段 {name!r}；允许的字段是 {PROPOSAL_FIELDS}")
    proposals = case.proposals

    if name == "ready":
        return case.ready
    if name == "proposal_count":
        return len(proposals)
    if name == "valid_count":
        return sum(1 for item in case.validations if item.valid)
    if name == "invalid_reasons":
        return [reason for item in case.validations for reason in item.reasons]
    if name == "security_flagged_count":
        # 按**提案**计数，不是按理由计数：越界目标会同时收到两条理由，
        # 用 `all_contain` 会因为那条"不在允许范围内"而永远不成立。
        return sum(
            1
            for item in case.validations
            if any(SECURITY_BOUNDARY_PHRASE in reason for reason in item.reasons)
        )
    if name == "passed_checks":
        return [check for item in case.validations for check in item.passed_checks]
    if name == "targets":
        return [item.target for item in proposals]
    if name == "evidence_ids":
        return [eid for item in proposals for eid in item.evidence_ids]
    if name == "evidence_id_count":
        return max((len(item.evidence_ids) for item in proposals), default=0)
    if name == "sample_count":
        return max((item.sample_count for item in proposals), default=0)
    if name == "sample_count_is_the_union":
        # 三条指标看的是**同一批帖子**，所以帖子数必须小于"各指标 n 之和"。
        # 生成路径若把它们相加，这里会红——那正是"把同一句话数三遍"。
        return _sample_count_is_union(case)
    if name == "rationale":
        return proposals[0].rationale if proposals else ""
    if name == "all_rationales":
        return [item.rationale for item in proposals]
    if name == "causal_markers":
        return [
            marker for item in proposals for marker in find_causal_language(item.rationale)
        ]
    if name == "instruction_markers":
        return [
            marker
            for item in proposals
            for marker in find_instruction_language(item.rationale)
        ]
    if name == "absolute_markers":
        # 与 `validate_proposal` 里的判据逐字一致（casefold，不是 lower）。
        # 基准自己另写一套匹配规则的话，它证明的就是基准而不是生产代码。
        return [
            marker
            for item in proposals
            for marker in ABSOLUTE_MARKERS
            if marker in item.rationale.casefold()
        ]
    if name == "confidence_is_always_absent":
        return all(item.confidence is None for item in proposals)
    if name == "automatic_application":
        return any(item.automatic_application for item in proposals)
    if name == "current_recipe_versions":
        return [str(item.current_recipe_version) for item in proposals]
    if name == "proposed_recipe_versions":
        return [str(item.proposed_recipe_version) for item in proposals]
    if name == "versions_differ":
        return bool(proposals) and all(
            item.current_recipe_version is not None
            and item.proposed_recipe_version is not None
            and item.current_recipe_version != item.proposed_recipe_version
            for item in proposals
        )
    if name == "gate_forced_zero":
        # 闸门关着 ⇒ 零条。这是一条**蕴含**，不是"恰好这次是零"。
        return not case.ready or len(proposals) == 0
    raise AssertionError(name)  # pragma: no cover - 上面已经穷举


def _sample_count_is_union(case: ProposalCase) -> bool:
    """提案的 `sample_count` 是不是那几条比较**帖子并集**的大小。

    直接从证据 id 反查比较、再取并集重算一遍——不是"看起来比另一个数小"，
    而是"等于正确算法的结果"。
    """
    from app.learning.evidence import evidence_id

    if not case.proposals:
        return False

    reports = {
        category: report_for(category)
        for category in ("balanced_cohorts", "causal_language")
    }
    for proposal in case.proposals:
        wanted = set(proposal.evidence_ids)
        post_ids: set[str] = set()
        for report in reports.values():
            for comparison in report.comparisons:
                if comparison.valid and evidence_id(comparison) in wanted:
                    post_ids.update(comparison.group_a.post_ids)
                    post_ids.update(comparison.group_b.post_ids)
        # 一条证据都对不上时**必须返回 False**：那意味着这里根本没能重算，
        # 而不是"重算出来正好相等"。早先的写法在这种情况下会掉到末尾的
        # `bool(case.proposals)` 上、报告一个凭空的通过——一个"证不出问题"
        # 被当成了"没问题"。
        if not post_ids:
            return False
        if proposal.sample_count != len(post_ids):
            return False
    return True


def _satisfies(actual: Any, relation: str, expected: Any) -> bool:
    if relation not in RELATIONS:
        raise KeyError(f"未知关系词 {relation!r}；允许的是 {RELATIONS}")
    if relation == "==":
        return actual == expected
    if relation == "!=":
        return actual != expected
    if relation == "is":
        return actual is expected
    if relation == "<":
        return actual < expected
    if relation == "<=":
        return actual <= expected
    if relation == ">":
        return actual > expected
    if relation == ">=":
        return actual >= expected
    if relation == "empty":
        return len(actual) == 0
    if relation == "nonempty":
        return len(actual) > 0
    if relation == "contains":
        return expected in actual
    if relation == "not_contains":
        return expected not in actual
    if relation == "none_contain":
        return all(str(expected) not in str(item) for item in actual)
    if relation == "all_contain":
        return bool(actual) and all(str(expected) in str(item) for item in actual)
    raise AssertionError(relation)  # pragma: no cover


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------


def check_the_eight_categories_are_all_present() -> list[str]:
    """八类都在、都不重名，而且字段与关系词都合法。"""
    # 用 list 而不是 set 收集：set 会丢掉顺序，于是下面那条"顺序也要对"的断言
    # 在一个**恰好**排好序的哈希里看着是过的，换个解释器就红。
    seen: list[str] = []
    for case in PROPOSAL_CASES:
        if case.category in seen:
            raise AssertionError(f"类别重复：{case.category}")
        seen.append(case.category)
        if case.source not in ("generated", "handmade"):
            raise AssertionError(f"{case.category}: 未知来源 {case.source!r}")
        if len(case.validations) != len(case.proposals):
            raise AssertionError(f"{case.category}: 提案与校验结果数量不一致")
        if not case.expect:
            raise AssertionError(f"{case.category}: 没有任何预期")
        for name, relation, _ in case.expect:
            if name not in PROPOSAL_FIELDS:
                raise AssertionError(f"{case.category}: 未知字段 {name!r}")
            if relation not in RELATIONS:
                raise AssertionError(f"{case.category}: 未知关系词 {relation!r}")
    # 顺序也要对：`PROPOSAL_CATEGORIES` 是给 `selftest.py` 与报告用的清单，
    # 顺序不一致时"八类都在"这句话就开始骗人（报告里会和基准里对不上号）。
    if tuple(seen) != PROPOSAL_CATEGORIES or len(PROPOSAL_CATEGORIES) != 8:
        raise AssertionError(
            f"类别集合与工作单点名的八个不一致：{seen}"
            f"（期望 {list(PROPOSAL_CATEGORIES)}）"
        )
    return [f"{len(PROPOSAL_CASES)} 类场景，字段与关系词全部合法"]


def check_every_case_matches_its_expectations() -> list[str]:
    notes: list[str] = []
    for case in PROPOSAL_CASES:
        for name, relation, expected in case.expect:
            actual = _resolve(case, name)
            if not _satisfies(actual, relation, expected):
                raise AssertionError(
                    f"{case.category}: {name} {relation} {expected!r} 不成立"
                    f"（实际是 {actual!r}）—— {case.note}"
                )
        notes.append(f"{case.category}: {case.note}")
    return notes


def check_the_valid_template_is_actually_valid() -> list[str]:
    """三条反证所改的那份底稿**本身必须合法**。

    底稿若不合法，"改了目标就 invalid"这句话说明不了任何事——它本来就是
    invalid。所以这里先把底稿单独验一遍，并要求它通过**全部**判据。
    """
    validation = validate_proposal(_template())
    if not validation.valid:
        raise AssertionError(f"底稿本身就不合法：{validation.reasons}")
    missing = [check for check in VALIDATION_CHECKS if check not in validation.passed_checks]
    if missing:
        raise AssertionError(f"底稿没有过全部判据，缺：{missing}")
    return [f"底稿通过全部 {len(VALIDATION_CHECKS)} 条判据"]


def check_invalid_never_comes_out_of_generation() -> list[str]:
    """**生成路径永远不产出 invalid 的提案**（工作单第二十六节的核心）。

    手写的那三类是反证：证明 `validate_proposal` 真的会拦人。若那条路也全过，
    说明校验形同虚设——而"形同虚设的守卫比没有守卫更糟"。
    """
    generated_cases = [c for c in PROPOSAL_CASES if c.source == "generated"]
    handmade_cases = [c for c in PROPOSAL_CASES if c.source == "handmade"]
    if not generated_cases or not handmade_cases:
        raise AssertionError("生成与手写两条路径都必须有场景")

    generated = 0
    for case in generated_cases:
        for item, validation in zip(case.proposals, case.validations):
            generated += 1
            if not validation.valid:
                raise AssertionError(
                    f"{case.category}: 生成路径产出了 invalid 的提案 "
                    f"{item.id!r}：{validation.reasons}"
                )
    rejected = sum(
        1
        for case in handmade_cases
        for validation in case.validations
        if not validation.valid
    )
    total = sum(len(case.validations) for case in handmade_cases)
    if rejected != total:
        raise AssertionError(f"手写路径必须**全部**被拒，实际 {rejected}/{total}")

    # 金丝雀：上面几条断言都依赖 `SECURITY_BOUNDARY_PHRASE` 这句措辞真的还在
    # 生产代码里。生产改了措辞的话，`security_flagged_count == 5` 会变成 0，
    # 报出来的却是"安全边界没被点出来"——一个听起来像安全问题的假象。
    # 这里先把"措辞变了"这件事本身说清楚。
    import inspect

    from app.learning import proposal as proposal_module

    if SECURITY_BOUNDARY_PHRASE not in inspect.getsource(proposal_module):
        raise AssertionError(
            f"生产代码里已经找不到 {SECURITY_BOUNDARY_PHRASE!r} 这句措辞了；"
            "基准里断言安全边界的那几条需要跟着更新（在此之前它们的失败会"
            "伪装成安全问题）。"
        )
    return [
        f"生成路径 {generated} 条提案全部 valid",
        f"手写路径 {rejected}/{total} 条全部被拒（校验不是摆设）",
    ]


def check_the_gate_is_load_bearing() -> list[str]:
    """闸门关着时是零条，而且**闸门确实是那个原因**。

    两半：

    1. 三类闸门关着的场景，各自零条；
    2. **反证**——`balanced_cohorts` 本来能产出一条提案，把它的
       `learning_ready` 强行改成 False 之后必须变成零条。

    只有第 2 条能证明闸门是**承重**的。没有它，"零条"完全可以解释成"这批数据
    本来就推不出东西"（那三类正是如此），于是这条检查看着过了，其实什么也没守。
    """
    notes: list[str] = []
    for category in GATE_CLOSED_CATEGORIES:
        case = case_for(category)
        if case.proposals:
            raise AssertionError(f"{category}: 闸门关着却产出了提案")
        if case.ready:
            raise AssertionError(f"{category}: 这一类应当是闸门关着的")
        notes.append(f"{category}: 闸门关 → 0 条")

    report = report_for("balanced_cohorts")
    open_proposals = generate_proposals(report, base_recipe=base_recipe())
    if not open_proposals:
        raise AssertionError("balanced_cohorts 本该产出提案，反证无从谈起")
    closed = generate_proposals(
        report.model_copy(
            update={
                "readiness": report.readiness.model_copy(
                    update={"learning_ready": False}
                )
            }
        ),
        base_recipe=base_recipe(),
    )
    if closed:
        raise AssertionError(
            f"闸门关着仍然产出了 {len(closed)} 条提案——闸门不是硬门槛"
        )
    notes.append(
        f"反证：同一批数据开闸 {len(open_proposals)} 条、关闸 0 条（闸门承重）"
    )
    return notes


def check_already_in_effect_produces_nothing() -> list[str]:
    """基准配方**已经**在观察上更高的一侧时不产出提案。

    "值得尝试把 question 改成 question"是一条没有内容的建议。生成路径必须
    自己认出这件事，而不是指望人看出来。
    """
    profile = StyleProfileFeatures(
        sample_count=40,
        median_length=180.0,
        number_density=1.0,
        emoji_density=0.1,
        structure_distribution={"contrast": 0.5},
        hook_distribution={"question": 0.7, "statement": 0.3},
        opening_distribution={},
        closing_distribution={"statement": 0.7},
        sentence_length_distribution={},
    )
    proposals = generate_proposals(
        report_for("balanced_cohorts"), base_recipe=build_recipe(profile)
    )
    if proposals:
        raise AssertionError(
            f"基准配方已在更高的一侧，却仍产出 {len(proposals)} 条提案："
            f"{[p.proposed_value for p in proposals]}"
        )
    return ["基准配方 = 观察上更高的一侧 → 0 条（没有可尝试的变化）"]


def check_there_is_no_apply_path() -> list[str]:
    """包内**没有** `apply`：提案只被提出来，不被执行（工作单第一、三十五节）。

    对 `app/learning/` 做一次 AST 扫描，找任何以 `apply` 开头、或叫
    `execute` / `activate` / `deploy` 的顶层函数。给一个恒为 false 的布尔字段
    配一条真正的执行路径，是"约定"与"事实"最容易分家的地方。
    """
    import ast
    from pathlib import Path

    forbidden_prefixes = ("apply", "execute", "activate", "deploy", "enforce")
    package = Path(__file__).resolve().parents[2] / "app" / "learning"
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                forbidden_prefixes
            ):
                offenders.append(f"{path.name}::{node.name}")
    if offenders:
        raise AssertionError(f"app/learning/ 里出现了执行路径：{offenders}")
    return ["app/learning/ 里没有 apply / execute / activate / deploy / enforce"]


CHECKS: tuple[tuple[str, Any], ...] = (
    ("工作单点名的八类都在，字段与关系词合法", check_the_eight_categories_are_all_present),
    ("八类场景全部满足各自的预期", check_every_case_matches_its_expectations),
    ("三条反证的底稿本身合法（过全部判据）", check_the_valid_template_is_actually_valid),
    ("invalid ⇒ proposal = none（两条路径互为反证）", check_invalid_never_comes_out_of_generation),
    ("闸门关 ⇒ 硬零，而且闸门承重（反证）", check_the_gate_is_load_bearing),
    ("已经在建议的那一侧 ⇒ 不提案", check_already_in_effect_produces_nothing),
    ("没有 apply 路径（AST 扫描）", check_there_is_no_apply_path),
)


def run(*, verbose: bool = False, as_json: bool = False) -> int:
    results: list[dict[str, Any]] = []
    failures = 0
    for name, check in CHECKS:
        try:
            notes = check()
        except Exception as exc:  # noqa: BLE001 - 一条检查的失败不该中断其余检查
            failures += 1
            results.append(
                {"check": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
            )
            if not as_json:
                print(f"✗ {name}")
                print(f"    {type(exc).__name__}: {exc}")
            continue
        results.append({"check": name, "passed": True, "notes": notes})
        if not as_json:
            print(f"✓ {name}")
            if verbose:
                for note in notes:
                    print(f"    {note}")

    if as_json:
        print(
            json.dumps(
                {
                    "checks": results,
                    "cases": list(PROPOSAL_CATEGORIES),
                    "passed": len(CHECKS) - failures,
                    "failed": failures,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"\n学习提案离线基准：{len(CHECKS) - failures} 通过 / {failures} 失败"
            f"（共 {len(CHECKS)} 项，覆盖 {len(PROPOSAL_CASES)} 类场景）"
        )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/learning/proposals.py",
        description="学习提案的离线基准（不碰 X、不碰 LLM、不碰数据库）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条检查的说明")
    args = parser.parse_args(argv)
    return run(verbose=args.verbose, as_json=args.json)


if __name__ == "__main__":  # pragma: no cover - 入口
    sys.exit(main())
