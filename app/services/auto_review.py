"""自动审阅者的判据——**纯函数，零 IO，零 LLM 调用**。

这是"替人点批准"这个动作的全部理智所在。它回答一个问题：

    这一条稿子，我敢不敢替人放行？

## 为什么必须是确定性的

同 `app/agent/budget.py` 与学习层的闸门：**能不能放行不能交给模型自觉**。
一个被要求"谨慎判断"的模型，在它觉得这条还不错的时候仍然会放行——而闸门存在
的意义恰恰是在模型判断失误时**仍然能拦住**。所以这里没有一次模型调用，
每个条件都是 Python 算得出来的。

Critic 的分数与结论**是**模型给的，但它们进来时已经是**输入**（一次评审的产物），
不是本层的判断。本层不决定"写得好不好"，只决定"现有证据够不够我替人担这个责"。

## 一条刻意的不对称

   通过   → 才真正动作（正式模式下会发出去）
   不通过 → **什么都不做**，Run 留在队列里等人

误判的代价不对称：一次错误的拒绝只多一条队列记录；**一次错误的批准就是一条
已经发出去的烂帖**。所以不通过**不等于拒绝**——它只是"我不替你担这个责"，
决定权原样退回给人。`AutoReviewDecision.approved=False` 读作"未获自动放行"，
不读作"被否决"。

## 判据之间没有权重

一条不过就是不过，没有加分、没有综合分。同学习层禁止"用一个数代表好坏"的
口径：一个加权总分在这里会被读成"多少分以上就没问题"，而那个分数没有任何人
校准过。

## 影子模式下必须先跑的那个坑

判据里的重复检测要看**近期写过什么**（含草稿），不能只看**发过什么**。
影子模式什么都不发，只看已发布内容的话语料恒为空、判据恒通过，
量出来的一致率毫无意义。调用方传什么进 `recent_contents` 是这件事的关键，
见 `app/repositories.list_recent_draft_contents` 的说明。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from app.agent.nodes.finalize import (
    AgentIncompleteError,
    AgentOverlongError,
    pick_final_candidate,
)
from app.agent.post_structure import length_detail, over_hard_limit
from app.models.auto_review import VERDICT_APPROVE, VERDICT_REJECT
from app.services.automation_config import AutomationConfig
from app.style.gate import evaluate_content_gate
from app.style.repetition import evaluate_repetition

logger = logging.getLogger(__name__)

#: 只有这些 guardrail 原因码是**提示性**的，不阻止自动放行。
#:
#: 其余一律拦——**包括将来新增的、这里还不认识的原因码**。方向是刻意的：
#: 多拦一条的代价是进人工队列（人扫一眼就过去了），漏拦一条的代价是一条烂帖
#: 已经发到了 X 上。新的提示性检查若忘了登记在这里，表现为"自动发布变保守了"，
#: 那是一个会被人发现的、可修的偏差；反过来则是静默的。
NON_BLOCKING_GUARDRAIL_REASONS: frozenset[str] = frozenset(
    {
        # `too_long` 留在这里，是**因为它已经是多余的那一条**，不是因为长度不要紧。
        #
        # guardrail 的 `too_long` 是个布尔式的提示：只知道"超了"，不知道超多少、
        # 也不知道超的是哪条候选。长度这件事现在有专门的判据（下面 `evaluate_auto_publish`
        # 的第 2 条），它用同一个尺子（`over_hard_limit`）算出确切字数，并给出
        # 具名理由。同一件事报两遍、其中一遍还说不清，只会让 `reasons` 变长。
        #
        # 所以要保证的**不是**"这个码继续不拦"，而是"长度一定有人拦"——那由那条
        # 具名判据负责，且它读的是与发布路径同一个上限。谁哪天把那条判据删了，
        # 这里就会同时失去唯一的拦阻者；改这里之前先看那条判据还在不在。
        "too_long",
    }
)


@dataclass(frozen=True)
class AutoReviewDecision:
    """一次自动审阅的结论。

    `checks` 记录**每条判据的原始取值**，而不只是"过没过"。转正式前要回答
    "哪条判据在误拦"，只知道"被拒了"是回答不了的。
    """

    approved: bool
    reasons: tuple[str, ...] = ()
    checks: dict[str, Any] = field(default_factory=dict)
    critic_score: float | None = None

    @property
    def verdict(self) -> str:
        return VERDICT_APPROVE if self.approved else VERDICT_REJECT


def resolve_delivered_verdict(
    state: Mapping[str, Any], *, max_length: int | None = None
) -> tuple[str | None, Any | None]:
    """找出**实际交付的那条候选**，以及它自己的评审结论。

    这是本模块最容易写错的一处。`state["critic_result"]` 是"最近一次评价里
    **该关注的那条**"，而 `finalize` 交付的是 `pick_final_candidate` 选出来的
    **另一条**——改过稿、有 reject、或关注的那条不是最高分的非 reject 时，
    两者会不同。

    直接读 `critic_result.decision` 会**放行一条 Critic 从没说过 accept 的稿**。
    所以这里复用 `finalize.pick_final_candidate` 本身（不重新实现排序——
    两份排序实现迟早会分叉，而分叉的表现是"自动放行的那条和实际发布的那条
    不是同一条"），拿到 candidate_id 再取它的裁决。

    `max_length` 必须与 finalize 用**同一个值**，理由同上：`pick_final_candidate`
    在给了上限时会优先选不超长的那条，让本函数按纯分数选，就可能盯上**另一条**
    候选的裁决——于是"自动放行的那条"与"实际交付的那条"分叉，而这次分叉的
    方向是危险的（放行了一条并未交付的稿子的分数）。调用方（`evaluate_auto_publish`）
    统一从 `runtime.persona.max_length` 取。

    返回 `(candidate_id, CriticResult | None)`。第二条为 `None` 表示这条候选
    **从没被 Critic 评价过**——那种情况下判据是**不放行**（见 evaluate_auto_publish）。
    """
    try:
        chosen = pick_final_candidate(state, max_length=max_length)
    except AgentOverlongError as exc:
        # 候选池里没有一条合格。**不是"没有候选"**，但本函数的返回值表达不了
        # 这个区别（调用方只认 `None`），所以差异写进日志，理由由
        # `evaluate_auto_publish` 的长度判据给出——那条判据本来就是为这件事留的。
        logger.warning("auto review: %s", exc)
        return None, None
    except AgentIncompleteError:
        # 没有候选就没有交付，也就没有可以放行的东西。
        # 正常流程到不了这里（finalize 会先抛），但本函数是判据的入口，
        # 不该因为一个退化状态把 tick 整个炸掉。
        logger.warning("auto review: no candidate to evaluate")
        return None, None

    verdicts = {r.candidate_id: r for r in (state.get("critic_results") or [])}
    return chosen.candidate_id, verdicts.get(chosen.candidate_id)


def evaluate_auto_publish(
    *,
    content: str | None,
    state: Mapping[str, Any],
    guardrail_reasons: Sequence[str] = (),
    fact_ids: Iterable[str] | None = None,
    recent_contents: Sequence[str] = (),
    recent_topics: Iterable[str] = (),
    publish_attempts_today: int = 0,
    config: AutomationConfig,
    max_length: int | None = None,
) -> AutoReviewDecision:
    """逐条判据跑一遍。任一不过 → `approved=False`，理由收进 `reasons`。

    参数里的 `state` 是 LangGraph checkpoint 的 `values`（要从中解析**交付候选**
    的评审结论，见 `resolve_delivered_verdict`）。

    `max_length` 是正文的**交付硬上限**（`persona.max_length`），有两个用途：
    判定这条正文能不能自动放行，以及让"该看哪条候选"与 finalize 保持同一个口径。
    传 `None` 表示调用方没给出上限（手写 state 的单测、旧调用方）——那时**不判长度**：
    宁可少判一条，也不要拿一个猜出来的上限去拦。
    """
    checks: dict[str, Any] = {}
    reasons: list[str] = []

    # --- 1. 内容门禁：与发布路径**同一个函数** -----------------------------
    #
    # 调 `evaluate_content_gate` 而不是自己写一套正则：发布时拦下的理由和这里
    # 放行的理由必须是同一张清单。各写一份，最后会变成"自动放行了、发布被 422"。
    violations = evaluate_content_gate(content, fact_ids=fact_ids)
    checks["content_gate"] = {
        "passed": not violations,
        "violations": [v.code for v in violations],
    }
    if violations:
        reasons.extend(v.detail for v in violations)

    # --- 2. 交付硬上限：与发布路径**同一个尺子** ---------------------------
    #
    # 与上一条同样的道理：自动放行必须**保证发布能过**。发布侧现在会拒绝超长
    # 正文（`PublishService._preflight`），若这里放行、那里被拦，得到的就是
    # `MANUAL_GO_LIVE_STEPS` 里点名要避免的组合——一条"机器批准了、却发不出去"
    # 的记录，还会污染影子期的一致率，让转正式的门槛读到一个假的达标。
    #
    # 为什么不用 guardrail 的 `too_long`：那是个布尔提示，说不出超了多少字。
    # 这里用 `over_hard_limit` 自己算——它与发布侧、Critic、finalize 是同一个尺子。
    over = None if max_length is None else over_hard_limit(content or "", max_length)
    checks["length"] = (
        {"passed": True, "checked": False, "why": "调用方没有给出上限，本层不判"}
        if over is None
        else {
            "passed": not over,
            "checked": True,
            "limit": max_length,
            "value": len(content or ""),
        }
    )
    if over:
        reasons.append(
            f"{length_detail(content or '', max_length)}，超过交付硬上限，不自动放行。"
        )

    # --- 3. Guardrail 的硬性问题 -------------------------------------------
    #
    # 只看**不是**提示性的那些码。刻意不读 `guardrail_passed`：那个布尔值
    # 会被 `too_long` 拉成 False。长度**确实**是交付条件（上面第 2 条判据就是
    # 它），但它在这里是提示性的——那条判据用同一个尺子算出确切字数、给出具名
    # 理由，比一个只有真假的布尔值说得多。两件事分开写，是为了让"长度"这一条
    # 在 `checks` 里只出现一次、且说的是完整的那一次。
    blocking = [r for r in guardrail_reasons if r not in NON_BLOCKING_GUARDRAIL_REASONS]
    checks["guardrail"] = {
        "passed": not blocking,
        "blocking": list(blocking),
        "advisory": [r for r in guardrail_reasons if r in NON_BLOCKING_GUARDRAIL_REASONS],
    }
    if blocking:
        reasons.append(
            "Guardrail 命中了硬性问题（" + "、".join(blocking) + "）。"
        )

    # --- 4. 交付候选的评审结论 ---------------------------------------------
    #
    # `max_length` 一路带下去：`pick_final_candidate` 给了上限时会优先挑不超长的
    # 那条，这里必须与 finalize 用同一个值，否则盯上的可能是**另一条**候选。
    candidate_id, verdict = resolve_delivered_verdict(state, max_length=max_length)
    score = getattr(verdict, "score", None)
    decision = getattr(verdict, "decision", None)
    checks["critic"] = {
        "passed": verdict is not None and decision == "accept",
        "candidate_id": candidate_id,
        "decision": decision,
        "score": score,
    }
    if verdict is None:
        # 没被评价过的内容不放行：Critic 没见过它，本层没有任何依据替人担保。
        #
        # 唯一不报这句的情形：候选池里一条不超长的都没有（第 2 条已经拦下并说清了
        # 原因）。那时 `resolve_delivered_verdict` 返回 `None` 不是"没评价过"，
        # 是"没有可评价的"——照原样报会给出一个与事实不符的理由，而理由是要给人看的。
        if over:
            checks["critic"]["note"] = "候选池里没有不超长的正文，无从取评审结论"
        else:
            reasons.append(
                "交付的候选没有 Critic 评审记录——没有依据替人放行，交给人看。"
            )
    elif decision != "accept":
        reasons.append(
            f"Critic 对交付候选的结论是「{decision}」，不是 accept，交给人看。"
        )

    # --- 5. 分数下限 --------------------------------------------------------
    # 只在有分数时判；没分数的情况已经被第 3 条拦下了，不在这里重复报。
    score_ok = score is None or score >= config.min_critic_score
    checks["critic_score"] = {
        "passed": score_ok,
        "value": score,
        "threshold": config.min_critic_score,
    }
    if not score_ok:
        reasons.append(
            f"交付候选的 Critic 分数 {score:.1f} 低于自动放行门槛 "
            f"{config.min_critic_score:.1f}，交给人看。"
        )

    # --- 6. 跨帖重复 --------------------------------------------------------
    #
    # 这是整个自动发布里最要紧的一条：重复/近似重复内容会被 X 判为垃圾信息，
    # 而无人值守意味着没人会在发出去之前发现它。
    repetition = evaluate_repetition(
        content=content,
        topic=state.get("selected_topic"),
        recent_contents=recent_contents,
        recent_topics=recent_topics,
    )
    checks["repetition"] = {
        "passed": not repetition.blocks,
        "hits": [
            {"layer": h.layer, "evidence": h.evidence} for h in repetition.hits
        ],
        # lexical 层**仅供参考**，不参与判断——理由见 app/style/repetition.py。
        "lexical_containment": repetition.lexical_containment,
        "lexical_nearest": repetition.lexical_nearest,
    }
    if repetition.blocks:
        reasons.extend(h.detail for h in repetition.hits)

    # --- 7. 当日发布额度 ----------------------------------------------------
    #
    # 计入的是**尝试**次数而不是成功次数：结果未知的那次可能已经发出去了。
    # 这是最后一道兜底——tick 入口还有一道，见 auto_publish.py。
    cap_ok = publish_attempts_today < config.max_publishes_per_day
    checks["volume_cap"] = {
        "passed": cap_ok,
        "value": publish_attempts_today,
        "limit": config.max_publishes_per_day,
    }
    if not cap_ok:
        reasons.append(
            f"今天已经发起过 {publish_attempts_today} 次发布，达到上限 "
            f"{config.max_publishes_per_day}，今天不再自动放行。"
        )

    approved = not reasons
    return AutoReviewDecision(
        approved=approved,
        reasons=tuple(reasons),
        checks=checks,
        critic_score=score,
    )


# ---------------------------------------------------------------------------
# 影子模式的一致率
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgreementSummary:
    """自动判断与人的决定的对照。影子转正式的门槛就是读它。

    字段命名对应那个门槛里的每一项，所以界面上不需要再翻译一次。
    """

    decided_pairs: int = 0
    agreed: int = 0
    #: 自动放行 + 人否决 —— **最危险的那个错**，门槛要求它恒为 0。
    false_approve: int = 0
    #: 自动放行 + 人改稿 —— 正式模式下会发出一条人改过的原稿。
    soft_mismatch: int = 0
    #: 自动未放行 + 人照发或改后发 —— 只多一条队列记录。
    false_reject: int = 0
    auto_approvals: int = 0

    @property
    def agreement_rate(self) -> float | None:
        """一致率。没有配对时为 `None`——**不是 0**。

        "还没人审过"和"审过的全都不同意"在下游是两件事；把前者写成 0，
        界面上会显示成一个吓人的数字，而实际含义是"还没有数据"。
        同 5C 的 `NULL ≠ 0`。
        """
        if not self.decided_pairs:
            return None
        return self.agreed / self.decided_pairs

    @property
    def soft_mismatch_rate(self) -> float | None:
        """改稿率，分母是自动放行数。没有自动放行时为 `None`（同上，不是 0）。"""
        if not self.auto_approvals:
            return None
        return self.soft_mismatch / self.auto_approvals


#: 人的决定 → 它在对照里的含义。写下来是因为这三个词**不对等**：
#: approve 表示"原样可发"，edit 表示"不接受原样"（而正式模式发的正是原稿），
#: reject 表示"不该发"。把 edit 当成 approve 会让一致率虚高。
def summarize_agreement(counts: Mapping[tuple[str, str], int]) -> AgreementSummary:
    """把 `{(自动结论, 人的决定): 条数}` 折成对照表。

    只统计**已决**的配对（调用方的 SQL 已经保证了这一点，见
    `repositories.shadow_agreement_stats`）。
    """
    agreed = false_approve = soft_mismatch = false_reject = 0
    auto_approvals = 0
    decided = 0

    for (verdict, human), count in counts.items():
        decided += count
        if verdict == VERDICT_APPROVE:
            auto_approvals += count
            if human == "approve":
                agreed += count
            elif human == "edit":
                soft_mismatch += count
            else:  # reject，以及将来任何更严重的结论
                false_approve += count
        else:  # 自动未放行
            if human == "reject":
                agreed += count
            else:  # approve / edit：人认为这一条是可发的
                false_reject += count

    return AgreementSummary(
        decided_pairs=decided,
        agreed=agreed,
        false_approve=false_approve,
        soft_mismatch=soft_mismatch,
        false_reject=false_reject,
        auto_approvals=auto_approvals,
    )


# ---------------------------------------------------------------------------
# 影子 → 正式的门槛
# ---------------------------------------------------------------------------

#: 门槛的默认取值。写成常量而不是散在函数里，是因为它们会被反复引用，
#: 而"门槛是多少"必须只有一个出处——两处写不一样时，人看到的那一处往往不是
#: 真正在生效的那一处。
MIN_PAIRS_FOR_GO_LIVE = 20
MIN_DAYS_FOR_GO_LIVE = 7.0
MAX_SOFT_MISMATCH_RATE = 0.10
MIN_AGREEMENT_RATE = 0.90

#: 门槛里**机器算不出来**的那部分。刻意与 `criteria` 分开：
#:
#: 把它们混进同一张表会让 `ready=True` 读起来像"可以翻转了"，而它们恰恰是
#: "还有一步没做"。分开之后，`ready` 的含义被限定成"自动判据全过"，
#: 而这几条每次都会原样下发，不会因为自动判据全过就消失。
MANUAL_GO_LIVE_STEPS: tuple[str, ...] = (
    "对影子期每条自动通过的正文空跑一次 PublishService._preflight"
    "（影子期调用它没有副作用：它发生在 claim_publish 之前），确认没有"
    "「自动放行了、发布却 422」的组合。",
    "逐条看过每个 false_reject 的具名判据，并明确接受它带来的误拦率"
    "（工作单：若误拦集中在 lexical 层，转正式前直接关掉它）。",
    "确认 config/automation.yaml 里 max_publishes_per_day 仍是 1、"
    "min_critic_score 已按金丝雀要求抬高。",
)


@dataclass(frozen=True)
class GateCriterion:
    """门槛里的一条。`detail` 说明**为什么**是这个结论，不只是过没过。"""

    key: str
    label: str
    met: bool
    detail: str


@dataclass(frozen=True)
class GateReport:
    """影子 → 正式的可行性。

    `ready` 只表示**自动判据全过**，不表示"可以翻转了"——`manual_steps`
    每次都会一起下发，见那个常量的说明。
    """

    ready: bool
    criteria: tuple[GateCriterion, ...]
    manual_steps: tuple[str, ...] = MANUAL_GO_LIVE_STEPS

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "criteria": [
                {
                    "key": c.key,
                    "label": c.label,
                    "met": c.met,
                    "detail": c.detail,
                }
                for c in self.criteria
            ],
            "manual_steps": list(self.manual_steps),
        }


def evaluate_go_live_gate(
    summary: AgreementSummary,
    *,
    window_days: float,
    false_reject_reasons: Sequence[Sequence[str]],
    stale_claims: int,
    auto_approved_clean: bool,
    min_pairs: int = MIN_PAIRS_FOR_GO_LIVE,
    min_days: float = MIN_DAYS_FOR_GO_LIVE,
    max_soft_mismatch_rate: float = MAX_SOFT_MISMATCH_RATE,
    min_agreement_rate: float = MIN_AGREEMENT_RATE,
) -> GateReport:
    """把一致率数据判成"能不能转正式"。**纯函数**，数据由调用方查好。

    为什么把门槛也做成确定性的、而且**每条都要求具名证据**：这一步一旦做错，
    错的是真实账号。所以每一条都必须说得出来"我是根据哪个数判的"，
    而不是一个综合分。

    所有"还没有数据"的情形一律判**不过**（`met=False`），并说明是缺数据。
    与 `agreement_rate` 返回 `None` 而不是 0 是同一条纪律的两个面：
    `None` 不能冒充"零风险"。
    """
    criteria: list[GateCriterion] = []

    enough_sample = summary.decided_pairs >= min_pairs and window_days >= min_days
    criteria.append(
        GateCriterion(
            key="sample",
            label=f"至少 {min_pairs} 个已决配对，且观察满 {min_days:.0f} 天",
            met=enough_sample,
            detail=(
                f"已决配对 {summary.decided_pairs} 个，观察 {window_days:.1f} 天。"
            ),
        )
    )

    # **0/0 不是成绩。** 一条都没自动放行过时，"误放行 = 0" 什么也没证明：
    # 正式模式下机器真正放行的那条路径此刻完全没有被测过。分母取
    # `auto_approvals` 而不是 `decided_pairs`——这条判据问的是"放行过的那些里
    # 有没有被否决的"，分母自然就是放行数。同 `soft_mismatch_rate` 的口径。
    #
    # 少了这个前提，"转正式后最危险的那个错从没发生过"会在一个什么都没跑过的
    # 系统上显示成绿色达标——正是 `NULL ≠ 0` 那条纪律要防的东西，只是这次
    # 它出现在一个布尔判据上。
    approved_any = summary.auto_approvals > 0
    if not approved_any:
        false_approve_detail = f"还没有自动放行过任何一条（已决 {summary.decided_pairs} 个配对），这个 0 什么也没证明。"
    elif summary.false_approve == 0:
        false_approve_detail = f"自动放行过 {summary.auto_approvals} 次，没有一次被人否决。"
    else:
        false_approve_detail = (
            f"出现过 {summary.false_approve} 次——必须先查清是哪条判据漏了，"
            "再看要不要收紧，不能直接转正式。"
        )

    criteria.append(
        GateCriterion(
            key="false_approve",
            label="自动放行 + 人否决 = 0（硬阻断）",
            met=approved_any and summary.false_approve == 0,
            detail=false_approve_detail,
        )
    )

    rate = summary.soft_mismatch_rate
    criteria.append(
        GateCriterion(
            key="soft_mismatch",
            label=f"改稿率 ≤ {max_soft_mismatch_rate:.0%}",
            met=rate is not None and rate <= max_soft_mismatch_rate,
            detail=(
                "还没有任何一条自动放行过，改稿率无从判断——"
                "这**不是**通过。正式模式下的批准路径此刻完全没有被测过。"
                if rate is None
                else f"自动放行 {summary.auto_approvals} 条，其中 {summary.soft_mismatch} "
                f"条人做了修改，改稿率 {rate:.0%}。"
            ),
        )
    )

    agreement = summary.agreement_rate
    criteria.append(
        GateCriterion(
            key="agreement",
            label=f"一致率 ≥ {min_agreement_rate:.0%}",
            met=agreement is not None and agreement >= min_agreement_rate,
            detail=(
                "还没有已决配对。"
                if agreement is None
                else f"{summary.agreed}/{summary.decided_pairs} = {agreement:.0%}。"
            ),
        )
    )

    # 归因：每条误拦都得说得出是哪条判据拦的。一条没有理由的拒绝是**不可归因**的，
    # 门槛因此在这一点上不放行——不是因为它危险，而是因为它无法被评估。
    unattributed = [r for r in false_reject_reasons if not r]
    criteria.append(
        GateCriterion(
            key="attribution",
            label="每个误拦都能归因到具名判据",
            met=not unattributed,
            detail=(
                f"共 {len(false_reject_reasons)} 条误拦，全部带理由。"
                if not unattributed
                else f"{len(unattributed)}/{len(false_reject_reasons)} 条误拦没有留下任何理由，"
                "无法判断是哪条判据在拦。"
            ),
        )
    )

    criteria.append(
        GateCriterion(
            key="content_gate",
            label="每条自动放行的正文当时都通过了内容门禁",
            met=auto_approved_clean,
            detail=(
                "记录里的内容门禁检查全部为空。"
                if auto_approved_clean
                else "有自动放行的记录当时内容门禁非空——**这不应当发生**，"
                "说明记录与判据不一致，必须先查。"
            ),
        )
    )

    criteria.append(
        GateCriterion(
            key="stale_claims",
            label="没有无法解释的、缺 review 行的 approved 残留",
            met=stale_claims == 0,
            detail=(
                "干净。"
                if stale_claims == 0
                else f"还有 {stale_claims} 条残留——说明回收逻辑没生效，"
                "转正式前要先修好，否则正式模式下同样会卡住。"
            ),
        )
    )

    return GateReport(ready=all(c.met for c in criteria), criteria=tuple(criteria))


__all__ = [
    "MANUAL_GO_LIVE_STEPS",
    "MAX_SOFT_MISMATCH_RATE",
    "MIN_AGREEMENT_RATE",
    "MIN_DAYS_FOR_GO_LIVE",
    "MIN_PAIRS_FOR_GO_LIVE",
    "NON_BLOCKING_GUARDRAIL_REASONS",
    "AgreementSummary",
    "AutoReviewDecision",
    "GateCriterion",
    "GateReport",
    "evaluate_auto_publish",
    "evaluate_go_live_gate",
    "resolve_delivered_verdict",
    "summarize_agreement",
]
