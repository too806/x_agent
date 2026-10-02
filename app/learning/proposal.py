"""学习提案：把一条**观察**变成一条**可以尝试的方向**（Phase 5D 工作单第四至二十五节）。

这一层是 `app/learning/` 的最后一个纯模块，它做的事只有一件：

    LearningEvidence（过去观察到了什么）
        ↓
    LearningProposal（未来可以尝试什么）
        ↓
    Human Review（人批准 / 否决）

## 它**不**做的事，比它做的事更重要

    不自动修改 Prompt
    不自动修改 StyleRecipe
    不自动修改 Trend weights
    不自动修改 Viewpoint weights
    不自动修改 Critic threshold
    不自动修改 Budget
    不自动发布
    不自动上线策略

`app/learning/` 里**没有** `apply_learning()`，本模块里**没有** `apply()`。提案是
一条被记下来的建议，不是一次执行。`LearningProposal.automatic_application` 恒为
`False`，而且它是**字段**不是一句约定——约定不会让测试红，字段会。

## 为什么全是确定性的，不调 LLM（工作单第二十一节）

LLM 可以"解释、组织、表达"一条提案，但**不能**决定 `learning_ready`、样本够不够、
比较成不成立。那三件事在 5C 已经是 Python 判定；本层再让模型参与一次，等于给同一个
判断加开第二条会漂移的路径。所以这里连措辞都是固定模板——于是"这条提案是不是
在声称因果"能在出口处用一个词表**检查**掉，而不是靠读一遍。

## 三个数字，缺一个这条提案就不该被读

    sample_count    依据了多少条**帖子**（不是快照；同 5C 的分析单位）
    evidence_ids    依据了哪几条 LearningEvidence —— **空 = 直接非法**
    limitations     它的边界（观测性比较 / 选题没被控制 / 离群点 / 缺哪一维）

## `current_value` 说真话的规则

`current_value` 里的"current"是一个**关于生产配置的事实主张**，不是这条比较的另一侧。
所以它只有两种合法写法：

    base_recipe 在该目标上有取值   → 就写那个取值（这是配方真的在用的），
                                     并给出 current / proposed 两个配方版本号
    取不到基准                     → 写本次比较里中位数较低的一侧，**同时**在
                                     limitations 里写明"它不代表生产配置的当前取值"，
                                     且版本号一律为 `None`

把"观察上较低的那一侧"直接说成"当前就是它"，是拿一个观察冒充一个事实。另外两条
由此推出的边界：

* 基准取值**已经等于**观察上更高的一侧 → **不产出提案**。没有可尝试的变化时，
  输出一条"把 question 改成 question"的建议，比输出零条更糟。
* 基准取值不在比较的两侧（例如配方用 statistic，而比较是 statement vs question）
  → 照常提案，但 limitations 里说明这条观察**没有比较过**基准的那个取值。

## 允许的目标是**推得出来的**目标

`DIMENSION_TARGETS` 的像集与 `ProposalTarget` 完全相等。一项"理论上允许、但没有任何
观察维度能推到"的目标不会出现在这里——工作单第八节要求提案必须引用证据，
没有证据能推到的目标就是一条没有依据的建议。

`scope_exceeds_evidence` **刻意没有目标**：它标记的是"结论超出证据"，属于 Critic /
Guardrail 那条安全线（5C 里写明按 P0/P1 处理）。工作单第十六节要求提案绝对不能
影响安全规则——所以它只被观察，不被建议改变。
"""

from __future__ import annotations

from typing import Sequence

from app.feedback.signals import (
    MIN_SAMPLES_FOR_COMPARISON,
    find_causal_language,
    find_instruction_language,
)
from app.learning.cohort import dimension_label, metric_label
from app.learning.evidence import evidence_id
from app.learning.schema import (
    CohortDimension,
    LearningEvidence,
    LearningEvidenceKind,
    LearningProposal,
    LearningReport,
    ObservedComparison,
    ProposalStatus,
    ProposalTarget,
    ProposalValidation,
)
from app.style.recipe import recipe_version
from app.style.schemas import StyleRecipe
from app.style.taxonomy import HookType, StructureType

#: 一次最多提出几条。工作单第二十二节：数量很小，最多 2～3。
#: 每条证据最多产出一条提案，所以这个上限同时也是"最多看几条证据"。
MAX_PROPOSALS = 3

#: 提案的 id 前缀。**确定性**：同一条证据每次得到同一个 id，于是"人上次裁决过
#: 的那一条"与"这次生成出来的同一条"能对上——这是 Review 能叠加回来的前提。
PROPOSAL_ID_PREFIX = "proposal:"

#: 目标名里出现这些片段，一律判为**安全 / 生产边界**（工作单第六、十六节）。
#:
#: 这是一张**检查表**：`validate_proposal()` 用它把一个越界的目标连原因一起拒绝，
#: 而不是让它在构造时抛一个没有解释的校验错误。凡是命中它的提案，
#: 无论措辞多干净，都是 `invalid`。
FORBIDDEN_TARGET_MARKERS: tuple[str, ...] = (
    "trend",
    "guardrail",
    "publish",
    "budget",
    "critic",
    "security",
    "safety",
    "review",
    "cas",
    "threshold",
    "persona",
    "prompt",
)

#: **绝对化措辞**的封闭标记表（工作单第二十四节）。
#:
#: 与 `app.feedback.signals.CAUSAL_MARKERS` 分开放在这里，而不是加进那张表：
#: 那张表被 5C 的每一句话共用，改它等于改 5C 的语义（工作单第二十七节要求
#: 保留全部既有语义）。本表只作用于提案的措辞。
ABSOLUTE_MARKERS: tuple[str, ...] = (
    "一定",
    "必然",
    "必定",
    "保证",
    "确保",
    "绝对",
    "肯定",
    "guarantee",
    "guaranteed",
    "definitely",
    "certainly",
)

#: 观察维度 → 允许被建议的目标。**像一个集恰好等于 `ProposalTarget`。**
#:
#: 没有 `SCOPE_EXCEEDS_EVIDENCE`：见模块抬头。没有 `LENGTH_BUCKET` 之外的其他
#: 维度，因为本仓库只有这六个确定性维度。
DIMENSION_TARGETS: dict[CohortDimension, ProposalTarget] = {
    CohortDimension.STRUCTURE_TYPE: ProposalTarget.STYLE_STRUCTURE,
    CohortDimension.HOOK_TYPE: ProposalTarget.STYLE_HOOK,
    CohortDimension.LENGTH_BUCKET: ProposalTarget.STYLE_LENGTH,
    CohortDimension.STANCE: ProposalTarget.VIEWPOINT_STANCE,
    CohortDimension.COUNTERPOINT_AWARENESS: ProposalTarget.VIEWPOINT_COUNTERPOINT,
}

#: 目标 → 人读名。展示层与文案共用，不在各处再写一遍。
TARGET_LABELS: dict[str, str] = {
    ProposalTarget.STYLE_STRUCTURE.value: "写作结构",
    ProposalTarget.STYLE_HOOK.value: "开头方式",
    ProposalTarget.STYLE_LENGTH.value: "正文长度档",
    ProposalTarget.VIEWPOINT_STANCE.value: "判断强度",
    ProposalTarget.VIEWPOINT_COUNTERPOINT.value: "是否带反方视角",
}

#: 目标 → StyleRecipe 上对应的字段（只对有对应字段的目标写）。
#:
#: 长度**不在表内**：`length_bucket` 是 short / medium / long 三档，而
#: `StyleRecipe.observed_target_length` 是一个**字数**。两者之间没有本仓库认可的
#: 确定性换算，硬编一个（short→80）就是在提案里塞进一个没人核对过的数字。
#: 于是这一类提案**不给版本号**，并在 limitations 里说明——本仓库同一条纪律见
#: 5C 对 `topic class` 维度的处理：缺就写下来，不假装有。
TARGET_RECIPE_FIELDS: dict[str, str] = {
    ProposalTarget.STYLE_STRUCTURE.value: "structure_type",
    ProposalTarget.STYLE_HOOK.value: "hook_type",
}

#: 目标 → 把 cohort 取值强成 recipe 字段类型的构造函数。
_TARGET_COERCIONS: dict[str, object] = {
    ProposalTarget.STYLE_STRUCTURE.value: StructureType,
    ProposalTarget.STYLE_HOOK.value: HookType,
}

#: 校验判据的**封闭**名字表。测试与展示按它取用。
VALIDATION_CHECKS: tuple[str, ...] = (
    "has_evidence",
    "evidence_is_a_comparison",
    "sample_sufficient",
    "window_consistent",
    "target_allowed",
    "not_security_related",
    "not_causal",
    "not_absolute",
    "not_a_recommendation",
    "confidence_absent",
    "automatic_application_false",
)

#: 每一份提案都带着的底线说明。措辞刻意避开因果词、绝对词与指令词——
#: 提案里的每一句都要经得起同一套检查。
BASE_PROPOSAL_LIMITATIONS: tuple[str, ...] = (
    "这是一次观察性比较，没有对照组、没有实验设计，不构成任何因果结论。",
    "点赞、回复、转发、引用是四类不同的行为，本提案不把它们合成单一分数，"
    "也不把其中任何一个当作内容质量的度量。",
    "本提案只建议**尝试**，不表示已经证明这样做更好，也不表示系统会自动采用它。",
)


def _fmt(value: float | None) -> str:
    """整数按整数印，方便人读（中位数可能落在两个整数中间）。未知 → 「未知」。"""
    if value is None:
        return "未知"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def proposal_id(dimension: str, window: str) -> str:
    """一个（维度，窗口）→ 它那条提案的确定性 id。

    **刻意不用证据 id**：同一个维度上的三类活动计数各产出一条证据，而它们聚合
    成**一条**提案（见 `_build_for_target`）。用主证据的 id 做提案 id，会让 id
    随"哪条指标排在第一"变化，而人上次裁决的那一条就再也对不上了。

    形如 `proposal:hook_type@24-72h`：稳定、可读、与窗口绑定——换一个观察窗口
    就是另一条提案，因为"发布后 2 小时"与"发布后 48 小时"不是同一件事。
    """
    return f"{PROPOSAL_ID_PREFIX}{dimension}@{window}"


def _comparison_of(
    report: LearningReport, evidence_id_value: str
) -> ObservedComparison | None:
    """按证据 id 找回那一条**成立**的比较。

    用 `evidence_id()` 反查而不是给 `LearningEvidence` 加字段：证据的形状已经被
    5C 的测试钉死，加字段会动到那一层的语义（工作单第二十七节）。反查是纯函数，
    且 id 与比较是一一对应的。
    """
    for comparison in report.comparisons:
        if comparison.valid and evidence_id(comparison) == evidence_id_value:
            return comparison
    return None


def _proposed_recipe(
    base_recipe: StyleRecipe | None, target: str, value: str
) -> StyleRecipe | None:
    """把基准 recipe 的**一个**字段换成提案的取值，用来算建议版本号。

    基准 recipe 缺失、或该目标没有对应的 recipe 字段（长度 / 观点类）时返回
    `None`——这时版本号一律为空，并在 limitations 里说明。**绝不用一个默认
    recipe 顶替**：那会让"没有基准"看起来像"基准是一份中性配方"。
    """
    if base_recipe is None:
        return None
    field = TARGET_RECIPE_FIELDS.get(target)
    if field is None:
        return None
    coerce = _TARGET_COERCIONS[target]
    try:
        typed = coerce(value)  # type: ignore[operator]
    except ValueError:
        # 取值不在 recipe 的枚举里（例如 cohort 里留着一个旧标签）。版本号
        # 干脆不给，而不是把它悄悄归成一个默认值——后者会算出一个假的版本。
        return None
    return base_recipe.model_copy(update={field: typed})


def _rationale(
    comparison: ObservedComparison,
    target: str,
    current_value: str,
    proposed_value: str,
    *,
    supporting_metrics: int,
    baseline_known: bool,
) -> str:
    """提案的理由。**固定模板**，措辞经得起 `validate_proposal` 的逐条检查。

    四段：

    1. 事实——窗口、维度、两组各自的 n 与中位数（都是观察到的数）；
    2. 方向——只说"在本样本中观察到哪一侧更高"，不说谁"更好"；
    3. 支撑——这个方向由几条指标同向支持（只有一条时不写这句）；
    4. 边界——这是观察性比较、不构成因果、只建议尝试。

    `baseline_known` 决定第 4 段里那句括号**怎么写**：只有基准配方真的取这一侧时
    才能写"当前为 X"。取不到基准时写"当前取值未知"——把观察上较低的那一侧
    说成"当前就是它"，是拿一个观察冒充一个事实。
    """
    a, b = comparison.group_a, comparison.group_b
    label = TARGET_LABELS.get(target, target)
    current_clause = f"当前为 {current_value}" if baseline_known else "当前取值未知"
    parts = [
        f"在 {comparison.observation_window} 观察窗口下，按"
        f"{dimension_label(comparison.dimension)}分组，"
        f"{a.value}（n={a.sample_count}）的{metric_label(comparison.metric)}中位数为 "
        f"{_fmt(a.median)}，{b.value}（n={b.sample_count}）为 {_fmt(b.median)}。"
        f"在本样本中观察到「{proposed_value}」一侧更高。"
    ]
    if supporting_metrics > 1:
        parts.append(
            f"这个方向在 {supporting_metrics} 类活动计数上的观察是一致的"
            "（不一致的方向不会写成提案）。"
        )
    parts.append(
        "这是观察到的差异，不构成因果结论，也不能说明哪一种写法更好；"
        f"它只说明在下一步里值得尝试把「{label}」取 {proposed_value}"
        f"（{current_clause}）再观察一次，看这个方向是否还会出现。"
    )
    return "".join(parts)


def _direction(comparison: ObservedComparison) -> str | None:
    """一条比较的观察方向：中位数较高的那一侧的取值；无方向时 `None`。

    中位数相等时返回 `None`——说某一侧"值得尝试"是在编一个不存在的差异。
    """
    a, b = comparison.group_a, comparison.group_b
    if a.median is None or b.median is None:  # pragma: no cover - valid 蕴含中位数存在
        return None
    if a.median == b.median:
        return None
    return b.value if b.median > a.median else a.value


def _build_for_target(
    dimension: CohortDimension,
    target: ProposalTarget,
    pairs: Sequence[tuple[LearningEvidence, ObservedComparison]],
    *,
    window: str,
    base_recipe: StyleRecipe | None,
    min_samples: int,
) -> LearningProposal | None:
    """一个目标上的全部观察 → 一条提案（或 `None`）。

    ## 为什么是"一个目标一条"，而不是"一条证据一条"

    同一个维度会在三类活动计数上各产出一条比较（点赞 / 对话 / 转发）。
    把它们各写成一条提案，界面上就会出现三条**改同一个旋钮、方向也相同**的建议
    ——那不是三个选项，是同一条建议被数了三遍。所以这里按目标聚合。

    ## 方向不一致就不提案

    如果点赞说 A 更高、转发说 B 更高，那么"该往哪边试"这件事从这批数据里
    **分不出来**。此时正确的输出是零条提案，而不是挑一个指标来支持一个方向。

    ## 样本量按**帖子并集**算，不把三组相加

    三条指标上的观察看的是**同一批帖子**。相加等于把同一句话数了三遍，
    正是 5C 一开始就拒绝的那件事。
    """
    usable = [(e, c) for e, c in pairs if c.group_a.median is not None and c.group_b.median is not None]
    if not usable:
        return None

    directions = {_direction(c) for _, c in usable}
    # `None` 表示那一条上没有观察到方向；不同的方向值表示指标之间不一致。
    if None in directions or len(directions) != 1:
        return None
    observed_higher = directions.pop()
    if observed_higher is None:  # pragma: no cover - 上面已经排除
        return None

    primary_evidence, primary = usable[0]
    observed_lower = (
        primary.group_b.value if observed_higher == primary.group_a.value else primary.group_a.value
    )

    # `current_value` 必须说真话：它要么是**基准配方真的在用的**取值，要么
    # 明确标注"不代表生产配置"。绝不把"观察上较低的那一侧"直接写成"当前是它"。
    baseline = _recipe_field_value(base_recipe, target)
    has_baseline = baseline is not None
    if has_baseline and baseline == observed_higher:
        # 配方已经就在观察上更高的那一侧：没有可尝试的变化，不产出提案。
        return None
    current_value = baseline if has_baseline else observed_lower
    proposed_value = observed_higher

    post_ids: set[str] = set()
    outlier_count = 0
    evidence_ids: list[str] = []
    for evidence, comparison in usable:
        evidence_ids.append(evidence.id)
        post_ids.update(comparison.group_a.post_ids)
        post_ids.update(comparison.group_b.post_ids)
        outlier_count += comparison.group_a.outlier_count + comparison.group_b.outlier_count

    limitations: list[str] = list(primary_evidence.limitations)
    limitations.extend(BASE_PROPOSAL_LIMITATIONS)
    limitations.append(
        "本阶段不对「信息密度 / 结尾方式 / 语气」提案：本仓库没有它们的确定性观察"
        "维度，缺这一项不被假装成没有差异。"
    )
    if target is ProposalTarget.STYLE_LENGTH:
        limitations.append(
            "长度档（short / medium / long）与 StyleRecipe 的目标**字数**之间没有"
            "本仓库认可的确定性换算，因此本提案不给配方版本号，也不声称"
            "「该写多少字」。"
        )

    # 建议版本号要**成对**给出：只有基准版本、没有建议版本，等于说了一半。
    proposed_recipe = (
        _proposed_recipe(base_recipe, target.value, proposed_value) if has_baseline else None
    )
    has_versions = has_baseline and proposed_recipe is not None

    if not has_baseline:
        limitations.append(_no_baseline_note(base_recipe, target, observed_lower))
        limitations.append(
            "本次给不出配方版本号——没有可信的基准，算出来的版本号只是一个"
            "看起来精确的假数字。"
        )
    elif not has_versions:
        limitations.append(
            f"目标取值 {proposed_value} 不在 StyleRecipe 该字段的取值范围内，"
            "因此本提案给不出建议版本号（也就无法回答「换了之后是哪一版」）。"
        )
    if has_baseline and baseline not in (observed_lower, observed_higher):
        # 配方在用的取值**不在**这次比较的两侧：这条观察并没有比较过它。
        limitations.append(
            f"基准配方当前取 {baseline}，它不在这条比较的两侧"
            f"（{observed_lower} / {observed_higher}）里；本提案说明的是"
            f"「{observed_higher} 在本样本中更高」，而不是"
            f"「把 {baseline} 换成 {observed_higher} 会怎样」。"
        )

    proposal = LearningProposal(
        id=proposal_id(dimension.value, window),
        target=target.value,
        current_value=current_value,
        proposed_value=proposed_value,
        rationale=_rationale(
            primary,
            target.value,
            current_value,
            proposed_value,
            supporting_metrics=len(usable),
            baseline_known=has_baseline,
        ),
        evidence_ids=tuple(evidence_ids),
        # 帖子数取**并集**：三条指标看的是同一批帖子，相加会把同一句话数三遍。
        sample_count=len(post_ids),
        observation_window=window,
        comparison_group=primary.comparison_group,
        confidence=None,
        limitations=tuple(dict.fromkeys(limitations)),
        status=ProposalStatus.PROPOSED,
        current_recipe_version=recipe_version(base_recipe) if has_versions else None,
        proposed_recipe_version=recipe_version(proposed_recipe) if has_versions else None,
        outlier_count=outlier_count,
        automatic_application=False,
    )
    if not validate_proposal(proposal, report=None, min_samples=min_samples).valid:
        return None
    return proposal


def _recipe_field_value(base_recipe: StyleRecipe | None, target: ProposalTarget) -> str | None:
    """基准配方在**这个目标**上的取值；该目标不在 StyleRecipe 上时 `None`。"""
    if base_recipe is None:
        return None
    field = TARGET_RECIPE_FIELDS.get(target.value)
    if field is None:
        return None
    raw = getattr(base_recipe, field, None)
    if raw is None:
        return None
    return str(getattr(raw, "value", raw))


def _no_baseline_note(
    base_recipe: StyleRecipe | None, target: ProposalTarget, fallback_value: str
) -> str:
    """「基准取不到」的**具体**原因。两种原因分开说，因为处理方式不同：
    一种要人去补 Profile，另一种是这层本来就够不着它。"""
    label = TARGET_LABELS.get(target.value, target.value)
    if base_recipe is None:
        return (
            "本次没有可用的基准 StyleRecipe，因此 `current_value` 取本次比较里"
            f"中位数较低的一侧（{fallback_value}）——**它不代表生产配置的当前取值**。"
        )
    return (
        f"「{label}」不在 StyleRecipe 的字段里，基准配方说明不了它的当前取值；"
        f"`current_value` 取本次比较里中位数较低的一侧（{fallback_value}）——"
        "**它不代表生产配置的当前取值**。"
    )


def generate_proposals(
    report: LearningReport,
    *,
    base_recipe: StyleRecipe | None = None,
    min_samples: int = MIN_SAMPLES_FOR_COMPARISON,
    max_proposals: int = MAX_PROPOSALS,
) -> tuple[LearningProposal, ...]:
    """一份学习报告 → 至多 `max_proposals` 条提案。**纯函数。**

    ## 闸门是硬门槛（工作单第七节）

    `learning_ready = False` 时**直接返回空**，一条都不生成。这不是"少生成几条"：
    数据不够可靠时，正确的输出是零条提案，而不是几条看起来还行的建议。
    候选返回空比被下游过滤掉更安全——下游可能忘了过滤。

    ## 每条提案都要能回到一条证据（工作单第八节）

    只从 `COHORT_DIFFERENCE` 类型的证据生成；`INSUFFICIENT_SAMPLE` /
    `MISSING_METRIC` / `WINDOW_MISMATCH` / `INVALID_COMPARISON` 都是"什么也没法说"
    的出口，它们**不产出提案**。生成后每一条再过一遍 `validate_proposal`，
    任何一条不过就丢弃——"invalid → proposal = none"（工作单第二十六节）。

    ## 顺序是确定的

    按报告里证据的固定顺序（先比较、后状态；比较按维度与指标的固定顺序），
    取前 `max_proposals` 条。同一份报告每次得到同一组提案（逐位相同），
    于是"上一次看到的那几条"与"这一次的"能对上。
    """
    if not report.readiness.learning_ready:
        return ()
    if report.readiness.automatic_modification:  # pragma: no cover - 恒 False
        return ()

    # 先把证据按维度归拢。三类活动计数在同一维度上的三条比较，属于同一条建议。
    grouped: dict[CohortDimension, list[tuple[LearningEvidence, ObservedComparison]]] = {}
    for evidence in report.evidence:
        if evidence.type is not LearningEvidenceKind.COHORT_DIFFERENCE:
            continue
        dimension = _dimension_of(evidence)
        if dimension is None or dimension not in DIMENSION_TARGETS:
            continue
        comparison = _comparison_of(report, evidence.id)
        if comparison is None:  # pragma: no cover - 证据由成立的比较生成
            continue
        grouped.setdefault(dimension, []).append((evidence, comparison))

    proposals: list[LearningProposal] = []
    # 顺序由 `DIMENSION_TARGETS` 的键序决定，而它与报告里维度的固定顺序一致。
    for dimension in DIMENSION_TARGETS:
        if len(proposals) >= max_proposals:
            break
        pairs = grouped.get(dimension)
        if not pairs:
            continue
        proposal = _build_for_target(
            dimension,
            DIMENSION_TARGETS[dimension],
            pairs,
            window=report.observation_window,
            base_recipe=base_recipe,
            min_samples=min_samples,
        )
        if proposal is not None:
            proposals.append(proposal)
    return tuple(proposals)


def _dimension_of(evidence: LearningEvidence) -> CohortDimension | None:
    """证据的维度（字符串）→ 封闭枚举。读不到或不认识 → `None`。"""
    if evidence.dimension is None:
        return None
    try:
        return CohortDimension(evidence.dimension)
    except ValueError:  # pragma: no cover - 维度来自封闭枚举
        return None


def validate_proposal(
    proposal: LearningProposal,
    report: LearningReport | None = None,
    *,
    min_samples: int = MIN_SAMPLES_FOR_COMPARISON,
) -> ProposalValidation:
    """一条提案的完整性检查（工作单第二十三节）。**返回结果，不抛异常。**

    `report` 为 `None` 时跳过两条需要报告的判据（证据是否真的存在、窗口是否与
    报告一致）——静态判据（目标是否允许、是否越界、措辞、样本量）照样全查。
    这样一条"目标写成 Guardrail"的提案，即使手上没有报告，也照样被拒绝，
    而且原因指向**目标越界**，而不是"没有报告所以查不了"。
    """
    passed: list[str] = []
    reasons: list[str] = []

    # --- 证据（工作单第八节）---
    if proposal.evidence_ids:
        passed.append("has_evidence")
    else:
        reasons.append(
            "本提案没有引用任何 LearningEvidence；没有依据的建议一律不成立。"
        )

    kind_ok = True
    if report is not None:
        by_id = {evidence.id: evidence for evidence in report.evidence}
        missing = [eid for eid in proposal.evidence_ids if eid not in by_id]
        if missing:
            kind_ok = False
            reasons.append(f"本提案引用了报告里不存在的证据 {missing!r}。")
        else:
            not_comparison = [
                eid
                for eid in proposal.evidence_ids
                if by_id[eid].type is not LearningEvidenceKind.COHORT_DIFFERENCE
            ]
            if not_comparison:
                kind_ok = False
                reasons.append(
                    f"证据 {not_comparison!r} 不是组间比较（而是「什么也没法说」"
                    "的那类出口），不能作为提案依据。"
                )
    if kind_ok and proposal.evidence_ids:
        passed.append("evidence_is_a_comparison")

    # --- 样本量（工作单第十八节）---
    if proposal.sample_count >= min_samples:
        passed.append("sample_sufficient")
    else:
        reasons.append(
            f"本提案依据的帖子数为 {proposal.sample_count}，低于比较下限 "
            f"{min_samples}；样本不足时不产出任何建议。"
            "这个下限是采集量的门槛，不是统计显著性判断。"
        )

    # --- 窗口（工作单第十九节）---
    if report is None or proposal.observation_window == report.observation_window:
        passed.append("window_consistent")
    else:
        reasons.append(
            f"本提案的观察窗口是 {proposal.observation_window}，"
            f"与报告的 {report.observation_window} 不一致；"
            "不同窗口的观察不放在一起比较。"
        )

    # --- 目标范围（工作单第六节）---
    allowed = {target.value for target in ProposalTarget}
    if proposal.target in allowed:
        passed.append("target_allowed")
    else:
        reasons.append(
            f"目标 {proposal.target!r} 不在允许提案的范围内。允许的目标只有："
            + "、".join(sorted(allowed))
            + "。"
        )

    # --- 安全 / 生产边界（工作单第十六节）---
    hits = _forbidden_markers(proposal.target)
    if not hits:
        passed.append("not_security_related")
    else:
        reasons.append(
            f"目标 {proposal.target!r} 命中安全或生产边界标记 {hits!r}："
            "Trend 权重、预算、Critic 阈值、发布逻辑、Guardrail、人工审核与 CAS "
            "都不接受提案，这一条永远不成立。"
        )

    # --- 措辞（工作单第二十四节）---
    causal = find_causal_language(proposal.rationale)
    if not causal:
        passed.append("not_causal")
    else:
        reasons.append(f"理由里出现因果措辞 {causal!r}；本层只说观察到的差异。")

    absolute = [marker for marker in ABSOLUTE_MARKERS if marker in proposal.rationale.casefold()]
    if not absolute:
        passed.append("not_absolute")
    else:
        reasons.append(f"理由里出现绝对化措辞 {absolute!r}；观察不能写成断言。")

    instructional = find_instruction_language(proposal.rationale)
    if not instructional:
        passed.append("not_a_recommendation")
    else:
        reasons.append(
            f"理由里出现指令性措辞 {instructional!r}；提案是「可以尝试什么」，"
            "不是「应该做什么」。"
        )

    # --- 两个恒空 / 恒假字段 ---
    if proposal.confidence is None:
        passed.append("confidence_absent")
    else:
        reasons.append(
            f"本提案带了一个未经校准的 confidence={proposal.confidence!r}；"
            "没有校准过的概率模型，编一个数字就是捏造。"
        )

    if not proposal.automatic_application:
        passed.append("automatic_application_false")
    else:  # pragma: no cover - 恒 False
        reasons.append("本提案声称会被自动应用；本阶段不存在任何自动应用路径。")

    return ProposalValidation(
        valid=not reasons, reasons=tuple(reasons), passed_checks=tuple(passed)
    )


def _forbidden_markers(target: str) -> list[str]:
    """目标名里命中的边界标记（去重，按表里的顺序）。"""
    lowered = target.casefold()
    return [marker for marker in FORBIDDEN_TARGET_MARKERS if marker in lowered]


def proposals_by_status(
    proposals: Sequence[LearningProposal],
) -> dict[ProposalStatus, tuple[LearningProposal, ...]]:
    """按状态分组。**顺序保持输入顺序**，不排序——排序会引入一个没有依据的优先级。"""
    grouped: dict[ProposalStatus, list[LearningProposal]] = {}
    for proposal in proposals:
        grouped.setdefault(proposal.status, []).append(proposal)
    return {status: tuple(items) for status, items in grouped.items()}


__all__ = [
    "ABSOLUTE_MARKERS",
    "BASE_PROPOSAL_LIMITATIONS",
    "DIMENSION_TARGETS",
    "FORBIDDEN_TARGET_MARKERS",
    "MAX_PROPOSALS",
    "PROPOSAL_ID_PREFIX",
    "TARGET_LABELS",
    "TARGET_RECIPE_FIELDS",
    "VALIDATION_CHECKS",
    "generate_proposals",
    "proposal_id",
    "proposals_by_status",
    "validate_proposal",
]
