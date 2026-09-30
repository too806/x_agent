"""从 Profile + 证据信号推导 StyleRecipe（Phase 5B.2 工作单第十九节）。

## 为什么是确定性的，不调 LLM

工作单第三十五节要求风格提取离线/预计算，Planner 只在**真正需要决策**时
才选"风格"，而不是逐个风格参数做决策。把 recipe 的产生做成一次 LLM 调用
会同时违反两条：

1. 每个参数本来就有确定性依据（Profile 里的统计量、证据包里的可见事实）。
   让模型"再选一次"等于把同一个判断做两遍；
2. 模型给的参数**无法复核**——"为什么选对比结构"的答案会变成
   "模型觉得合适"，而它既不可解释也不可调。

所以这里是纯函数：给定同一个 Profile 与同一组证据信号，永远得到同一个 recipe，
并且每个选择都带一条能在 Profile 里找到的数字的理由。

## 为什么不新增 AgentAction

recipe 是 **Writer input preparation**：在 `generate_candidates` 之前由确定性
步骤备好，不增加动作、不扩大预算（工作单第三十七节）。它由候选工具在渲染
prompt 前取一次，写进 state，Critic 与 revise 再读同一个对象。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.style.config import StyleConfig, load_style_config
from app.style.schemas import (
    DensityLevel,
    EndingType,
    EvidenceRequirement,
    StanceStrength,
    StyleProfileFeatures,
    StyleRecipe,
    Tone,
)
from app.style.taxonomy import HookType, StructureType

#: `profile_ref` 的前缀。`corpus@<computed_at>` 让 Run Detail 事后能回到
#: 那一版 Profile——没有一个可回指的引用，recipe 就是一组无法复核的参数。
CORPUS_PROFILE_REF_PREFIX = "corpus"


@dataclass(frozen=True)
class EvidenceSignals:
    """从 EvidencePack 派生的确定性信号。

    刻意只抽**三个能数的东西**，而不是把整个 EvidencePack 传进来：
    recipe 只需要知道"手上材料厚不厚、有没有反方、有没有数字"。
    传整个包会让这一层依赖 Agent 的 schema，而它本该与 Agent 解耦——
    它读的是统计与计数，不是推理产物。
    """

    fact_count: int = 0
    counterpoint_count: int = 0
    has_numeric_fact: bool = False
    unknown_count: int = 0


def evidence_signals(pack: object | None) -> EvidenceSignals:
    """从 EvidencePack（鸭子类型）抽信号。`None` 返回全零。

    用 `getattr` 而不是 `isinstance`：这一层不该为了读三个计数而 import
    `app.agent.schemas`。缺字段一律按 0 处理——证据包不完整时 recipe 退化成
    最保守的一档，而不是抛异常把整轮运行打断。
    """
    if pack is None:
        return EvidenceSignals()

    facts = getattr(pack, "facts", None) or []
    counterpoints = getattr(pack, "counterpoints", None) or []
    unknowns = getattr(pack, "unknowns", None) or []

    numeric = False
    for fact in facts:
        # **只看事实正文**，绝不 `str(fact)` 兜底：`EvidenceFact` 的 repr 里带着
        # `fact_id='f1'`，那个 `1` 会让"有数字"对**每一条**事实都为真——于是
        # 每轮都选数据型开头，而原因是一个 id 里的序号。宁可读不到文本
        # （返回 False）也不要读到一个自带的数字。
        if isinstance(fact, str):
            text = fact
        else:
            # `fact` 是 EvidenceFact 的真实字段名；`statement`/`text` 只是别名防御。
            text = getattr(fact, "fact", None) or getattr(fact, "text", None)
        if text and any(ch.isdigit() for ch in str(text)):
            numeric = True
            break

    return EvidenceSignals(
        fact_count=len(facts),
        counterpoint_count=len(counterpoints),
        has_numeric_fact=numeric,
        unknown_count=len(unknowns),
    )


def build_recipe(
    profile: StyleProfileFeatures | None,
    signals: EvidenceSignals | None = None,
    *,
    config: StyleConfig | None = None,
    profile_ref: str = "",
    hard_max_length: int | None = None,
) -> StyleRecipe:
    """Profile + 证据信号 → StyleRecipe。确定性，纯函数。

    `profile` 为 `None` 时（还没采到样本、或 Profile 未计算）返回一个
    **中性 recipe** 并如实说明理由。刻意不返回 `None`：调用方拿到一个
    中性 recipe 与拿到 `None` 会导致两条不同的代码路径，而其中一条
    （无 recipe）正是 Smoke 4 要保住的老行为。让"没有 Profile"这件事
    在 recipe 内部表达，调用方只有一条路径。

    `hard_max_length` 是**平台侧**的正文硬上限（`persona.max_length`）。
    它与语料目标**分开记**：语料说的是"这些人平时写多长"，平台说的是
    "我们最多能发多长"。把上限直接写进 `observed_target_length` 会让
    recipe 声称一条语料从未说过的结论（见 `StyleRecipe` 的说明）。
    不传表示"这一层不知道平台约束"，此时 `effective_target_length`
    就等于语料目标。
    """
    cfg = (config or load_style_config()).recipe
    signals = signals or EvidenceSignals()

    if profile is None or profile.sample_count == 0:
        return StyleRecipe(
            reasons=["未取得 Style Profile（语料为空或未计算），本次不施加结构约束。"],
            profile_ref=profile_ref,
            hard_max_length=hard_max_length,
        )

    reasons: list[str] = []

    structure = _choose_structure(profile, signals, reasons)
    hook = _choose_hook(profile, signals, reasons)
    observed_length = _choose_target_length(profile, cfg, reasons)
    density = _choose_density(profile, signals, cfg, reasons)
    stance = _choose_stance(signals, cfg, reasons)
    evidence_requirement = _choose_evidence_requirement(signals, cfg, reasons)
    tone = _choose_tone(profile, cfg, reasons)
    ending = _choose_ending(profile, signals, reasons)

    if (
        observed_length is not None
        and hard_max_length is not None
        and hard_max_length < observed_length
    ):
        # 冲突要**说出来**，而不是悄悄取小值。这句话是这三段语义唯一
        # 同时出现的地方——没有它，Run Detail 上就是一个 140，看不出
        # 它到底是语料说的还是平台压出来的。
        reasons.append(
            f"当前发布长度硬上限是 {hard_max_length} 字，小于语料目标 "
            f"{observed_length} 字；本次实际执行目标取 {hard_max_length} 字。"
            f"**这是平台约束的结果，不是语料的结论**——"
            f"语料观察到的是 {observed_length} 字。"
        )

    reasons.append(
        f"本次参考的是 {profile.sample_count} 条真实样本统计出的 Profile"
        f"（profile_ref={profile_ref or '未标注'}）。"
    )

    return StyleRecipe(
        structure_type=structure,
        hook_type=hook,
        observed_target_length=observed_length,
        hard_max_length=hard_max_length,
        density=density,
        stance_strength=stance,
        evidence_requirement=evidence_requirement,
        tone=tone,
        ending_type=ending,
        reasons=reasons,
        profile_ref=profile_ref,
    )


# ---------------------------------------------------------------------------
# 各参数的选择。每个函数都往 reasons 里写一条带数字的依据。
# ---------------------------------------------------------------------------


def _dominant(distribution: dict[str, float], default: str) -> tuple[str, float]:
    """占比最高的那一项。并列时取键序最小的——**保证确定性**：
    字典遍历顺序不该影响结果，否则同一份 Profile 会给出不同的 recipe。"""
    if not distribution:
        return default, 0.0
    best_label = min(distribution, key=lambda label: (-distribution[label], label))
    return best_label, distribution[best_label]


def _choose_structure(
    profile: StyleProfileFeatures, signals: EvidenceSignals, reasons: list[str]
) -> StructureType:
    dominant, share = _dominant(profile.structure_distribution, StructureType.UNCLASSIFIED.value)

    # 证据决定**例外**：手上有两条以上反方说法时，对比结构在事实上更站得住，
    # 即使它在语料里不是最常见的那一类。这是"结构服务于内容"，
    # 而不是"内容去凑结构"。
    if signals.counterpoint_count >= 2 and dominant != StructureType.CONTRAST.value:
        reasons.append(
            f"证据包里有 {signals.counterpoint_count} 条反方说法，采用对比结构"
            f"（语料里最常见的是 {dominant}，占比 {share:.0%}）。"
        )
        return StructureType.CONTRAST

    if share <= 0:
        reasons.append("语料没有可用的结构分布，本次不指定结构。")
        return StructureType.UNCLASSIFIED

    reasons.append(f"语料里 {dominant} 占 {share:.0%}，是最常见的组织方式，本次采用它。")
    return _as_structure(dominant)


def _choose_hook(
    profile: StyleProfileFeatures, signals: EvidenceSignals, reasons: list[str]
) -> HookType:
    dominant, share = _dominant(profile.hook_distribution, HookType.UNCLASSIFIED.value)

    # 有数字事实时优先用数据开头：这是工作单第十二节"具体事实优于笼统"的落点，
    # 也让开头不必依赖语料里恰好出现过什么 hook。
    if signals.has_numeric_fact and dominant != HookType.STATISTIC.value:
        reasons.append("证据包里有带数字的事实，开头用数据切入，比语料里最常见的写法更具体。")
        return HookType.STATISTIC

    if share <= 0:
        reasons.append("语料没有可用的开头分布，本次不指定开头类型。")
        return HookType.UNCLASSIFIED

    reasons.append(f"语料里 {dominant} 型开头占 {share:.0%}，本次采用它。")
    return _as_hook(dominant)


def _choose_target_length(
    profile: StyleProfileFeatures, cfg, reasons: list[str]
) -> int | None:
    """**语料**目标长度。取 Profile 的中位数，收敛到配置区间。

    用中位数而不是均值：一条超长样本能把均值拽走，而中位数回答的是
    "典型的一条多长"——那才是"该写多长"的依据。

    这里产出的永远是**观察值**，不含平台上限；上限由 `build_recipe`
    单独记在 `hard_max_length` 上，两者在 `effective_target_length`
    里才相遇。这段函数对 `persona.max_length` 一无所知，是刻意的：
    一个知道平台上限的长度推导，迟早会把上限写进"语料说了什么"。
    """
    if profile.median_length is None:
        reasons.append("语料没有可用的长度分布，本次不指定目标长度。")
        return None
    raw = int(round(profile.median_length))
    clamped = max(cfg.min_target_length, min(cfg.max_target_length, raw))
    if clamped != raw:
        reasons.append(
            f"语料长度中位数是 {raw} 字，收敛到配置区间 "
            f"[{cfg.min_target_length}, {cfg.max_target_length}]，取 {clamped} 字。"
        )
    else:
        reasons.append(f"语料长度中位数是 {raw} 字，本次目标 {clamped} 字。")
    return clamped


def _choose_density(
    profile: StyleProfileFeatures, signals: EvidenceSignals, cfg, reasons: list[str]
) -> DensityLevel:
    density = profile.number_density
    if density is None:
        level = DensityLevel.MEDIUM
        reasons.append("语料没有可用的数字密度，密度取中档。")
    elif density < cfg.density_low_below:
        level = DensityLevel.LOW
        reasons.append(f"语料每百字 {density:.2f} 个数字，低于 {cfg.density_low_below}，密度取低档。")
    elif density > cfg.density_high_above:
        level = DensityLevel.HIGH
        reasons.append(f"语料每百字 {density:.2f} 个数字，高于 {cfg.density_high_above}，密度取高档。")
    else:
        level = DensityLevel.MEDIUM
        reasons.append(f"语料每百字 {density:.2f} 个数字，密度取中档。")

    # 证据里完全没有事实时不允许高档密度——没有事实可写，高密度只会变成
    # 车轱辘话。这是"结构约束不能凭空要求内容"的一条边界。
    if level is DensityLevel.HIGH and signals.fact_count == 0:
        reasons.append("但证据包里没有可引用的事实，密度降为中档——空手写不出高密度。")
        return DensityLevel.MEDIUM
    return level


def _choose_stance(signals: EvidenceSignals, cfg, reasons: list[str]) -> StanceStrength:
    if signals.counterpoint_count >= cfg.stance_strong_at:
        reasons.append(f"证据包里有 {signals.counterpoint_count} 条反方说法，立场取强。")
        return StanceStrength.STRONG
    if signals.counterpoint_count >= cfg.stance_moderate_at:
        reasons.append(f"证据包里有 {signals.counterpoint_count} 条反方说法，立场取中。")
        return StanceStrength.MODERATE
    reasons.append("证据包里没有反方说法，立场取弱——不做没有对手的强判断。")
    return StanceStrength.SOFT


def _choose_evidence_requirement(
    signals: EvidenceSignals, cfg, reasons: list[str]
) -> EvidenceRequirement:
    if signals.fact_count >= cfg.evidence_strict_at:
        reasons.append(f"证据包里有 {signals.fact_count} 条事实，证据要求取严。")
        return EvidenceRequirement.STRICT
    if signals.fact_count >= cfg.evidence_required_at:
        reasons.append(f"证据包里有 {signals.fact_count} 条事实，证据要求为必需。")
        return EvidenceRequirement.REQUIRED
    reasons.append("证据包里没有事实，证据要求只能放宽为可选——但要如实说明。")
    return EvidenceRequirement.OPTIONAL


def _choose_tone(profile: StyleProfileFeatures, cfg, reasons: list[str]) -> Tone:
    emoji = profile.emoji_density
    if emoji is not None and emoji > cfg.conversational_emoji_above:
        reasons.append(f"语料每百字 {emoji:.2f} 个 emoji，语气取口语化。")
        return Tone.CONVERSATIONAL
    if _dominant(profile.structure_distribution, "")[0] == StructureType.DATA_READING.value:
        reasons.append("语料以数据解读类结构为主，语气取分析型。")
        return Tone.ANALYTICAL
    reasons.append("语料没有明显的口语化或分析型特征，语气取中性。")
    return Tone.NEUTRAL


def _choose_ending(
    profile: StyleProfileFeatures, signals: EvidenceSignals, reasons: list[str]
) -> EndingType:
    dominant, share = _dominant(profile.closing_distribution, "statement")
    if dominant == "question":
        reasons.append(f"语料里 {share:.0%} 的样本以问句收尾，本次也用问句结尾。")
        return EndingType.QUESTION
    if dominant == "call":
        reasons.append(f"语料里 {share:.0%} 的样本以行动号召收尾，本次也用。")
        return EndingType.CALL
    if signals.fact_count == 0 and signals.counterpoint_count == 0:
        reasons.append("证据不足，结尾只做观察陈述，不强行下结论。")
        return EndingType.OBSERVATION
    reasons.append(f"语料里 {share:.0%} 的样本以陈述收尾，本次用结论收尾。")
    return EndingType.CONCLUSION


def _as_structure(value: str) -> StructureType:
    try:
        return StructureType(value)
    except ValueError:
        # 分类法收敛过程中，语料里可能留着旧版本的标签。
        # 归成 unclassified 而不是抛：一个陈旧标签不该让整轮运行失败。
        return StructureType.UNCLASSIFIED


def _as_hook(value: str) -> HookType:
    try:
        return HookType(value)
    except ValueError:
        return HookType.UNCLASSIFIED


__all__ = ["CORPUS_PROFILE_REF_PREFIX", "EvidenceSignals", "build_recipe", "evidence_signals"]
