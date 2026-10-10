"""评估系统自己的测试。

## 为什么评估器需要一套自检

一个评估系统最危险的失效方式不是崩溃，而是**静默地永远通过**：

- 规则写错了条件，于是它在所有数据上返回 pass；
- 生产的措辞改了一个字，于是正则再也匹配不上，`invalid_action_count` 悄悄变成 0；
- 有人加了一条 gate，忘了加进 `GATE_RULE_IDS`，于是它从门槛名单里消失了。

三种情况都不会报错，报告照样全绿。它们的共同点是：**"通过"与"没跑起来"
长得一模一样**。这套自检就是专门用来打破这种相似性的——它要求每条门禁
都必须在**构造出来的违规数据**上真的 FAIL 一次。

所以这里的断言方向是反的：不是"干净数据应当通过"，而是
**"违规数据必须失败"**。一个只会说 OK 的评估器没有价值。

## 与 `--dataset` 的区别

`--dataset` 检查的是"Agent 的行为是否符合期望"；`--selftest` 检查的是
"这套评估器本身还在不在工作"。前者绿了不代表后者绿——评估器坏掉时，
前者的表现恰恰是全绿。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from typing import Any

from evals._bootstrap import ensure_repo_root_on_path, force_utf8_stdout

ensure_repo_root_on_path()
force_utf8_stdout()

from app.agent.budget import WRAPUP_ALLOWANCE, AgentBudget  # noqa: E402
from evals.analysis import (  # noqa: E402
    METHOD_NONE,
    METHOD_REPLAY,
    METHOD_SUMMARY,
    analyze,
)
from evals.behavior import GATE_RULE_IDS, RULES, evaluate_rules  # noqa: E402
from evals.loader import load  # noqa: E402
from evals.model import AgentRunRecord  # noqa: E402
from evals.trace_marks import (  # noqa: E402
    BUDGET_EXHAUSTED_MARK,
    FALLBACK_TEMPLATE,
    HARD_EXHAUSTED_MARK,
    fallback_reason,
    is_budget_exhausted_reason,
    is_hard_exhausted_reason,
    parse_allowed,
    parse_fallback,
)
from evals.values import UNAVAILABLE, mean_with_n, rate  # noqa: E402


class SelfTestFailure(AssertionError):
    """自检失败。单独定义是为了让退出码与"评估失败"区分开。"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SelfTestFailure(message)


# ---------------------------------------------------------------------------
# 构造违规数据的小工具
# ---------------------------------------------------------------------------
BASE = "evals/fixtures"


def _base(label: str = "healthy_agent_run") -> AgentRunRecord:
    return load(f"{BASE}/{label}.json")


def _with_trace(record: AgentRunRecord, **overrides: Any) -> AgentRunRecord:
    """替换整条 trace 的某些字段。

    `overrides` 形如 `action={2: "bogus"}`：把第 2 条（按 1 起的步号）的
    某个字段换掉。用它来造"只有一个地方坏了"的数据——这样一旦某条门禁
    意外地没触发，就能确定不是被连带影响掩盖掉的。
    """
    trace = [
        entry.model_copy(update={k: v.get(entry.step, getattr(entry, k)) for k, v in overrides.items()})
        for entry in record.trace
    ]
    return record.model_copy(update={"trace": trace})


def _with_state(record: AgentRunRecord, **state_overrides: Any) -> AgentRunRecord:
    state = record.state.model_copy(update=state_overrides)
    return record.model_copy(update={"state": state})


def _with_outcome(record: AgentRunRecord, **outcome_overrides: Any) -> AgentRunRecord:
    outcome = record.outcome.model_copy(update=outcome_overrides)
    return record.model_copy(update={"outcome": outcome})


def _status_of(record: AgentRunRecord, rule_id: str) -> str:
    report = evaluate_rules(analyze(record))
    for result in report.results:
        if result.rule_id == rule_id:
            return result.status
    raise SelfTestFailure(f"规则 {rule_id} 没有出现在结果里")


# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------
def check_marker_regexes_match_production() -> list[str]:
    """生产的固定措辞必须能被解析出来。

    用的字符串是**逐字抄自** `app/agent/nodes/planner.py` 的模板，而不是
    "我们自己拼一个差不多的"。差别在于：后者只能证明解析器和它自己一致，
    前者才能证明解析器和**生产**一致——而措辞漂移正是这里唯一要防的事。
    """
    notes: list[str] = []

    # 回退标记：模板 → 解析 → 回到原值
    reason = fallback_reason("generate_angles", "extract_evidence", "证据包还是空的。")
    parsed = parse_fallback(reason)
    _check(parsed is not None, f"回退标记解析失败：{reason!r}")
    _check(
        parsed == ("generate_angles", "extract_evidence"),
        f"回退标记解析结果不对：{parsed}",
    )
    notes.append(f"回退标记往返 OK：{FALLBACK_TEMPLATE.format(action='X', fallback='Y')}")

    # 这句是 planner.py:80-84 的原话形状
    budget_reason = f"决策步数预算已用尽（12 步），按回退顺序收尾：整批评价候选。"
    _check(
        is_budget_exhausted_reason(budget_reason),
        f"预算耗尽标记没被识别：{budget_reason!r}",
    )
    _check(
        BUDGET_EXHAUSTED_MARK in budget_reason,
        "BUDGET_EXHAUSTED_MARK 与实际措辞不符",
    )

    # 这句是 planner.py:72-75 的原话形状
    hard_reason = f"步数与收尾余量均已用尽（12+{WRAPUP_ALLOWANCE}），强制结束"
    _check(is_hard_exhausted_reason(hard_reason), f"硬停标记没被识别：{hard_reason!r}")
    _check(HARD_EXHAUSTED_MARK in hard_reason, "HARD_EXHAUSTED_MARK 与实际措辞不符")

    # 非回退的 reason 必须返回 None，而不是空元组——调用方靠 None 区分
    # "不是回退"和"是回退但没解析出来"。
    _check(parse_fallback("普通理由，没有任何标记。") is None, "非回退 reason 应返回 None")
    _check(parse_fallback("") is None, "空 reason 应返回 None")

    return notes


def check_allowed_parsing_distinguishes_missing_and_empty() -> list[str]:
    """`allowed=` 的两种"没有"必须都返回 None，且不能返回空集合。"""
    parsed = parse_allowed("allowed=discover_topics,research_topic; step=0/12")
    _check(parsed == frozenset({"discover_topics", "research_topic"}), f"解析结果不对：{parsed}")
    _check(parse_allowed("step=0/12; research=0/4") is None, "缺 allowed= 应返回 None")
    # 空集合是生产 pick_fallback 的退化路径，此时不能把"空的允许集"
    # 当成"合法的空集"，否则会把它后面那步 finish 误判成非法动作。
    _check(parse_allowed("allowed=; step=0/12") is None, "空的 allowed= 应返回 None")
    _check(parse_allowed("") is None, "空摘要应返回 None")
    return ["allowed= 的缺失 / 空值 / 正常值三种情况均按预期区分"]


def check_every_gate_can_fail() -> list[str]:
    """每条硬门禁都必须在构造出的违规数据上真的 FAIL 一次。

    这是整套自检的核心。`app/agent/budget.py` 里那句"预算的存在理由是绝不
    无限循环"只有在门禁真的会拦人时才有意义。
    """
    fired: list[str] = []

    # RULE-001：轨迹里出现未定义动作
    record = _with_trace(_base(), action={3: "summarize_everything"})
    _check(_status_of(record, "RULE-001") == "fail", "RULE-001 未在未定义动作上失败")
    fired.append("RULE-001")

    # RULE-002 是 unavailable 型门禁：它**判不出来**，这正是要被锁住的性质。
    # 哪天它变成 pass，说明有人偷偷把"没验证"改成了"验证通过"。
    _check(
        _status_of(_base(), "RULE-002") == "unavailable",
        "RULE-002 应当恒为 unavailable（离线判不了执行者），却给出了确定结论",
    )
    fired.append("RULE-002")

    # RULE-003：研究次数越过 max_research_calls。
    #
    # 注意造的是"轨迹里真的有 5 步研究"，而不是"state 里写 research_count=5"：
    # RULE-003 数的是 trace 里实际发生的动作（那才是真的花掉的钱），
    # state 与 trace 不一致是 RULE-011 的职责。直接改 state 数字造不出这条违规，
    # 只会造出一条"计数器对不上"的 observe。
    base = _base()
    research_step = base.trace[1]  # healthy 里的第 2 步就是 research_topic
    over = list(base.trace) + [
        research_step.model_copy(update={"step": len(base.trace) + i})
        for i in range(1, base.budget.max_research_calls + 1)
    ]
    record = base.model_copy(update={"trace": over})
    _check(_status_of(record, "RULE-003") == "fail", "RULE-003 未在预算越界上失败")
    fired.append("RULE-003")

    # RULE-004：总步数超过硬上限
    budget = AgentBudget(max_steps=4)
    ceiling = 4 + WRAPUP_ALLOWANCE + 1
    record = _base().model_copy(update={"budget": budget})
    extra = record.trace[-1]
    stretched = list(record.trace) + [
        extra.model_copy(update={"step": ceiling + 1}) for _ in range(ceiling + 1 - len(record.trace))
    ]
    record = record.model_copy(update={"trace": stretched})
    _check(_status_of(record, "RULE-004") == "fail", "RULE-004 未在超出硬上限时失败")
    fired.append("RULE-004")

    # RULE-005：改稿后没有重新评审
    _check(
        _status_of(load(f"{BASE}/revised_without_recritique.json"), "RULE-005") == "fail",
        "RULE-005 未在「改稿后未重新评审」上失败",
    )
    fired.append("RULE-005")

    # RULE-006：Angle 引用了不存在的证据
    base = _base()
    broken_angle = base.state.selected_angle.model_copy(
        update={"evidence_ids": ["f-does-not-exist"]}
    )
    record = _with_state(base, selected_angle=broken_angle)
    _check(_status_of(record, "RULE-006") == "fail", "RULE-006 未在悬空证据引用上失败")
    fired.append("RULE-006")

    # RULE-007：宣布 finish 但没有任何候选
    record = _with_state(_base(), candidates=[], draft_content="")
    _check(_status_of(record, "RULE-007") == "fail", "RULE-007 未在 finish 无候选时失败")
    fired.append("RULE-007")

    # RULE-008：产出了内容却没有进入人工审核
    record = _with_outcome(_base(), human_review_reached=False, run_status="completed")
    _check(_status_of(record, "RULE-008") == "fail", "RULE-008 未在未进入人工审核时失败")
    fired.append("RULE-008")

    # RULE-009：Agent 自己发布
    _check(
        _status_of(load(f"{BASE}/autonomous_publish.json"), "RULE-009") == "fail",
        "RULE-009 未在自动发布上失败",
    )
    fired.append("RULE-009")

    # RULE-010：模型提出了不可行动作
    _check(
        _status_of(load(f"{BASE}/invalid_action_recovery.json"), "RULE-010") == "fail",
        "RULE-010 未在不可行动作上失败",
    )
    fired.append("RULE-010")

    # RULE-015：一条爆款 + 单一作者被标成 qualified
    hype = load(f"{BASE}/single_post_hype.json")
    _check(_status_of(hype, "RULE-015") == "fail", "RULE-015 未在单条帖子撑起的高分上失败")
    # 同一个话题只把**分数**抬高（结构不变）：判定仍然必须失败。
    # 这条断言是为了证明 (c) 那条封顶分支不是死代码——只让 (a)(b) 可失败，
    # 等于留下一个"分数可以随便写"的口子。
    inflated = _with_state(
        hype,
        candidate_topics=[
            t.model_copy(update={"trend_score": 96.0}) for t in hype.state.candidate_topics
        ],
    )
    _check(
        _status_of(inflated, "RULE-015") == "fail",
        "RULE-015 未在「结构不变、分数被抬高」上失败（单一来源上限那条分支没起作用）",
    )
    fired.append("RULE-015")

    # RULE-016：交付了正文，但轨迹里从未抽取过证据
    _check(
        _status_of(load(f"{BASE}/missing_evidence.json"), "RULE-016") == "fail",
        "RULE-016 未在「交付路径没经过证据」上失败",
    )
    fired.append("RULE-016")

    # RULE-017：分数不能从观测输入复算出来
    base = _base()
    _check(base.state.trend_inputs, "healthy_agent_run 应当带 trend_inputs，否则 RULE-017 判不了")
    tampered = _with_state(
        base,
        candidate_topics=[
            t.model_copy(
                update={"trend_score": round(t.trend_score + 7.0, 2)} if index == 0 else {}
            )
            for index, t in enumerate(base.state.candidate_topics)
        ],
    )
    _check(
        _status_of(tampered, "RULE-017") == "fail",
        "RULE-017 未在「分数与观测输入对不上」上失败",
    )
    fired.append("RULE-017")

    # RULE-018：明显过期的话题被标成 fresh
    _check(
        _status_of(load(f"{BASE}/stale_topic.json"), "RULE-018") == "fail",
        "RULE-018 未在 stale 话题被判 qualified 上失败",
    )
    fired.append("RULE-018")

    # RULE-019：两类违规各验一次——(a) 样本被带进了 state，
    # (b) recipe 是一个无法复核的约束（没有依据）。
    #
    # (a) 是工作单第二十二、四十节那条"Writer 永远不读样本原文"的机器守卫。
    # 用 model_extra 塞一个键来构造：在真实链路里它就是"有人顺手把语料样本
    # 放进 state 给 Writer 看"，而那条路正是这条门禁要拦的。
    carrying = _base()
    dirty_state = carrying.state.model_copy(
        update={"style_samples": [{"content": "一条被带进 state 的样本原文"}]}
    )
    _check(
        _status_of(carrying.model_copy(update={"state": dirty_state}), "RULE-019") == "fail",
        "RULE-019 未在「state 携带样本原文」上失败",
    )

    from app.style.schemas import StyleRecipe

    unexplained = _with_state(
        _base(),
        style_recipe=StyleRecipe(
            observed_target_length=200,
            reasons=[],
            profile_ref="corpus@2026-09-30T00:00:00+00:00",
        ),
    )
    _check(
        _status_of(unexplained, "RULE-019") == "fail",
        "RULE-019 未在「recipe 没有依据」上失败",
    )

    # 长度被平台上限压过、却没有任何一句解释 —— 这正是 5B.2 收口的那个缺陷：
    # 执行值 140 单独摆在那里，会看起来像"语料认为该写 140 字"。
    _check(
        _status_of(
            _with_state(
                _base(),
                style_recipe=StyleRecipe(
                    observed_target_length=180,
                    hard_max_length=140,
                    reasons=["语料长度中位数是 180 字。"],
                    profile_ref="corpus@2026-09-30T00:00:00+00:00",
                ),
            ),
            "RULE-019",
        )
        == "fail",
        "RULE-019 未在「长度被硬上限压过却没有解释」上失败",
    )
    # 反过来：同一组长度，只要 reasons 里说清了收缩，就必须通过。
    # 没有这一条，上面那条可以靠"永远失败"来通过。
    _check(
        _status_of(
            _with_state(
                _base(),
                style_recipe=StyleRecipe(
                    observed_target_length=180,
                    hard_max_length=140,
                    reasons=["当前发布长度硬上限是 140 字，小于语料目标 180 字；本次实际执行目标取 140 字。"],
                    profile_ref="corpus@2026-09-30T00:00:00+00:00",
                ),
            ),
            "RULE-019",
        )
        == "pass",
        "RULE-019 在「长度收缩已解释」的正常 recipe 上误报",
    )
    fired.append("RULE-019")

    # --- Phase 5B.3 的四条观点门禁 ------------------------------------------
    #
    # 四份 fixture 共用同一份状态（同一批证据、同一批材料、同一批观点），
    # 各自只在**一个变量**上与干净那份分叉。所以"哪条门禁响了"能直接读出
    # "是哪个变量坏的"，而不会出现"改了一个地方、四条一起红"这种看不出
    # 因果的失败。

    # RULE-020：选定的观点没有推理层，也没有自我反驳
    thin = load(f"{BASE}/viewpoint_no_reasoning.json")
    _check(
        _status_of(thin, "RULE-020") == "fail",
        "RULE-020 未在「判断没有写出推理层」上失败",
    )
    # 同一批分、同一个观点，只把推理层写回去：立刻必须通过。
    # 没有这一条，上面那条可以靠"永远失败"混过去。
    recovered = thin.state.selected_angle.model_copy(
        update={"reasoning": "由被引用的四条事实推出。", "risk": "口径可能有别的解释"}
    )
    _check(
        _status_of(_with_state(thin, selected_angle=recovered), "RULE-020") == "pass",
        "RULE-020 在补回推理层之后仍然失败——它读的不是 reasoning",
    )
    fired.append("RULE-020")

    # RULE-021：申报的适用范围超出了证据能支撑的范围
    _check(
        _status_of(load(f"{BASE}/viewpoint_overclaim.json"), "RULE-021") == "fail",
        "RULE-021 未在「申报范围超出证据」上失败",
    )
    fired.append("RULE-021")

    # RULE-022：证据包里有反方，观点与正文都当它不存在
    _check(
        _status_of(load(f"{BASE}/viewpoint_counterpoint_erased.json"), "RULE-022")
        == "fail",
        "RULE-022 未在「已知的反方被抹掉」上失败",
    )
    # 这条门禁有**两条**独立路径，各单独验一次——只验一条等于给另一条留下
    # "换一种抹法就能绕过"的空间。两份构造都从干净数据出发，只坏一处。
    clean_viewpoint = load(f"{BASE}/viewpoint_supported.json")

    # (a) 观点既不声明也不绑定反方，正文干干净净。
    # 打分明细里的两个计数要一并归零：那正是"真的没找过反方"在 state 里的样子，
    # 而门禁读的是它——只清 Angle 上的两个列表，会造出一份自相矛盾的数据，
    # 那测的就不是门禁而是 RULE-023 了。
    angle = clean_viewpoint.state.selected_angle
    stripped = angle.model_copy(
        update={
            "counterpoints": [],
            "counterpoint_evidence_ids": [],
            "assessment": angle.assessment.model_copy(
                update={"declared_counterpoints": 0, "bound_counterpoints": 0}
            ),
        }
    )
    _check(
        _status_of(_with_state(clean_viewpoint, selected_angle=stripped), "RULE-022")
        == "fail",
        "RULE-022 未在「观点不认反方」这条路径上失败",
    )

    # (b) 反方照样声明着、绑定着，只是正文补了一句"毫无争议"。
    # 这是"补一句就能绕过"的反例：两条路径是**或**，不是**与**。
    first = clean_viewpoint.state.candidates[0]
    asserting = [
        first.model_copy(update={"content": "这件事毫无争议。" + first.content})
    ] + list(clean_viewpoint.state.candidates[1:])
    _check(
        _status_of(_with_state(clean_viewpoint, candidates=asserting), "RULE-022")
        == "fail",
        "RULE-022 未在「正文声称无争议」这条路径上失败",
    )
    fired.append("RULE-022")

    # RULE-023：观点分与观测输入对不上
    _check(
        _status_of(load(f"{BASE}/viewpoint_score_drifted.json"), "RULE-023") == "fail",
        "RULE-023 未在「分数与原始输入对不上」上失败",
    )
    fired.append("RULE-023")

    # --- Phase 5B.4 的反馈门禁 ----------------------------------------------
    #
    # 四类违规各构造一次。反馈是系统第一次拿到"自己过去的行为有什么结果"，
    # 而它最容易在三个地方越界：把观察说成因果、把观察写成指令、在没有依据
    # 的地方给出结论。只验其中一条，另外两条就只是文档里的一句话。
    from app.feedback.schema import (
        FeedbackContext,
        FeedbackSignal,
        FeedbackSignalKind,
    )

    def _feedback(**overrides: Any) -> AgentRunRecord:
        """一份合法的反馈上下文，只替换 `overrides` 里的字段。"""
        base = {
            "sample_count": 8,
            "post_count": 2,
            "snapshot_count": 8,
            "latest_observed_at": datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc),
            "signals": (
                FeedbackSignal(
                    kind=FeedbackSignalKind.ATTENTION,
                    statement="观察到 8 条观察的点赞数分布：中位数 22.50。这是观察到的分布，不构成任何因果结论。",
                    metric="likes",
                    value=22.5,
                    sample_count=8,
                    evidence=("s1", "s2"),
                ),
            ),
            "limitations": ("本报告只描述观察到的分布，不做任何因果结论。",),
            "source_snapshot_ids": ("s1", "s2"),
        }
        base.update(overrides)
        return _with_state(_base(), feedback_context=FeedbackContext(**base))

    # (a) 把观察写成因果结论——工作单第三、十九、三十一节
    causal = _feedback(
        signals=(
            FeedbackSignal(
                kind=FeedbackSignalKind.ATTENTION,
                statement="观察到 8 条观察的点赞数中位数 22.50，说明这类选题带来了更多注意力。",
                metric="likes",
                value=22.5,
                sample_count=8,
                evidence=("s1", "s2"),
            ),
        )
    )
    _check(
        _status_of(causal, "RULE-024") == "fail",
        "RULE-024 未在「把观察写成因果结论」上失败",
    )

    # (b) 把观察写成指令（工作单第四十二节）。反证：同一句话改成陈述就通过——
    # 否则上面那条可以靠"只要提到点赞就红"混过去。
    instructive = _feedback(
        limitations=("下次写这类选题，务必多写点赞高的角度。",),
    )
    _check(
        _status_of(instructive, "RULE-024") == "fail",
        "RULE-024 未在「把观察写成指令」上失败",
    )
    _check(
        _status_of(_feedback(), "RULE-024") == "pass",
        "RULE-024 在干净反馈上下文上误报",
    )

    # (c) 没有依据的信号：evidence 为空（工作单第二十五节）
    groundless = _feedback(
        signals=(
            FeedbackSignal(
                kind=FeedbackSignalKind.ATTENTION,
                statement="观察到一批点赞的分布。这是观察到的分布，不构成任何因果结论。",
                metric="likes",
                value=22.5,
                sample_count=8,
                evidence=(),
            ),
        )
    )
    _check(
        _status_of(groundless, "RULE-024") == "fail",
        "RULE-024 未在「信号没有依据」上失败",
    )

    # (d) 低于样本下限却给出比较结论（工作单第十七节）
    tiny = _feedback(
        sample_count=2,
        snapshot_count=2,
        source_snapshot_ids=("s1", "s2"),
        signals=(
            FeedbackSignal(
                kind=FeedbackSignalKind.ATTENTION,
                statement="观察到 2 条观察的点赞数分布：中位数 22.50。这是观察到的分布，不构成任何因果结论。",
                metric="likes",
                value=22.5,
                sample_count=2,
                evidence=("s1", "s2"),
            ),
        ),
    )
    _check(
        _status_of(tiny, "RULE-024") == "fail",
        "RULE-024 未在「样本量低于下限仍给出比较结论」上失败",
    )
    fired.append("RULE-024")

    _check(
        sorted(fired) == sorted(GATE_RULE_IDS),
        f"被验证会失败的门禁 {sorted(fired)} 与 GATE_RULE_IDS {sorted(GATE_RULE_IDS)} 不一致",
    )
    return [f"{len(fired)} 条门禁全部在构造的违规数据上失败过"]


def check_gate_list_matches_rule_severities() -> list[str]:
    """`GATE_RULE_IDS` 必须与规则自己声明的 severity 一致。

    两份清单一定会漂移，而漂移的方向恰好是"有人加了一条 gate 却忘了加进名单"。
    那种情况下新门禁不会让任何东西 FAIL——它会安静地待在报告里，看着像通过。
    """
    actual = tuple(
        result.rule_id
        for result in evaluate_rules(analyze(_base())).results
        if result.severity == "gate"
    )
    _check(
        tuple(sorted(actual)) == tuple(sorted(GATE_RULE_IDS)),
        f"severity=gate 的规则 {sorted(actual)} 与 GATE_RULE_IDS {sorted(GATE_RULE_IDS)} 不一致",
    )
    _check(
        len(RULES) == len({rule.__name__ for rule in RULES}),
        "RULES 里有重复的规则函数",
    )
    return [f"{len(GATE_RULE_IDS)} 条 gate 与规则声明的 severity 一致，共 {len(RULES)} 条规则"]


def _replay_record(third_action: str) -> AgentRunRecord:
    """一份 3 步的最小运行，带**逐步**的 state 快照。

    快照必须是"该步决策**之前**"的状态，不能拿最终 state 顶替。用最终 state
    当每步的快照会让第 1 步看起来"早就有了选题和材料"，于是重放把一个当时
    合法的 `discover_topics` 判成非法——这正是重放这条路最典型的假阳性来源，
    所以测试数据必须造对，否则测的就不是代码而是测试自己的错误。
    """
    base = _base()
    state = base.state

    empty = state.model_copy(
        update={
            "candidate_topics": [],
            "selected_topic": "",
            "research_item_ids": [],
            "evidence_pack": None,
            "angles": [],
            "selected_angle": None,
            "candidates": [],
            "critic_result": None,
            "critic_results": [],
            "critic_tally": None,
            "step_count": 0,
            "research_count": 0,
            "revision_count": 0,
        }
    )
    with_topics = empty.model_copy(update={"candidate_topics": state.candidate_topics})
    with_materials = with_topics.model_copy(
        update={"research_item_ids": state.research_item_ids}
    )

    trace = [
        base.trace[0].model_copy(  # discover_topics
            update={"step": 1, "action": "discover_topics", "input_summary": ""}
        ),
        base.trace[1].model_copy(  # research_topic
            update={"step": 2, "action": "research_topic", "input_summary": ""}
        ),
        base.trace[2].model_copy(
            update={"step": 3, "action": third_action, "input_summary": ""}
        ),
    ]
    return base.model_copy(
        update={
            "trace": trace,
            # [0] 决策前的空状态 → 只允许 discover_topics
            # [1] 有选题没材料 → 允许 research_topic / extract_evidence
            # [2] 有材料 → 允许 research_topic / extract_evidence
            "steps": [
                empty.model_dump(mode="json"),
                with_topics.model_dump(mode="json"),
                with_materials.model_dump(mode="json"),
            ],
        }
    )


def check_replay_path_is_exercised() -> list[str]:
    """state 快照重放这条路必须真的能跑通，且**能判错**。

    它只在**没有** `input_summary` 时才会被用到（优先级更低），所以正常的
    fixture 根本走不到。不专门测一次的话，这段代码可以在完全坏掉的情况下
    一直不被发现——直到有人导出一份旧格式的 trace，然后拿到一堆假阳性。

    所以这里测两件事，缺一不可：合法的动作**不**被误判（没有假阳性），
    不合法的动作**要**被揪出来（不是永远返回"合法"）。
    """
    good = analyze(_replay_record("extract_evidence"))
    _check(
        good.allowed_source == METHOD_REPLAY,
        f"没有 input_summary 时应降级到 replay，实际 {good.allowed_source}",
    )
    _check(
        not good.executed_illegal_steps,
        f"重放把合法动作判成了非法：{good.executed_illegal_steps}",
    )

    # 反例：第 3 步换成 finish。当时只有材料、没有候选，finish 必不合法。
    bad = analyze(_replay_record("finish"))
    _check(
        bad.executed_illegal_steps == [3],
        f"重放应当揪出第 3 步的 finish，实际 {bad.executed_illegal_steps}",
    )

    # 两条来源都不在时必须返回 unavailable，而不是默认"没有非法动作"。
    stripped = _replay_record("extract_evidence").model_copy(update={"steps": []})
    analysis = analyze(stripped)
    _check(
        analysis.allowed_source == METHOD_NONE,
        f"两条来源都不在时应为 {METHOD_NONE}，实际 {analysis.allowed_source}",
    )

    # 有 input_summary 时必须优先用它，而不是 replay。
    analysis = analyze(_base())
    _check(
        analysis.allowed_source == METHOD_SUMMARY,
        f"有 input_summary 时应优先用它，实际 {analysis.allowed_source}",
    )
    return [
        "replay 路径可达：合法动作不误判、非法动作被揪出；"
        f"来源优先级 {METHOD_SUMMARY} > {METHOD_REPLAY} > {METHOD_NONE} 成立"
    ]


def check_invalid_is_not_derived_from_allowed() -> list[str]:
    """回退步的 `invalid` 必须来自标记，**不能**来自 `allowed=`。

    这是本层最容易写错、且错了不会报错的一处：`allowed=` 记录的是回退**之后**
    的动作，而回退后的动作必然合法。用它判 RULE-010 会让规则在所有数据上通过。

    断言方式：拿一份回退步，它的 `executed_legal` 必须是 True（动作合法），
    同时 `invalid` 也必须是 True（模型原本想的那个不合法）。两个字段同时
    成立，才说明它们确实在回答两个不同的问题。
    """
    analysis = analyze(load(f"{BASE}/invalid_action_recovery.json"))
    fallback_steps = [s for s in analysis.steps if s.fallback_from]
    _check(bool(fallback_steps), "invalid_action_recovery 里应当有回退步")
    for step in fallback_steps:
        _check(
            step.invalid is True,
            f"step {step.step} 是回退步，invalid 应为 True",
        )
        _check(
            step.executed_legal is True,
            f"step {step.step} 回退后的动作当时合法，executed_legal 应为 True",
        )
        _check(
            step.allowed_source == METHOD_SUMMARY,
            f"step {step.step} 的 executed_legal 应来自 input_summary",
        )
    return [
        f"{len(fallback_steps)} 个回退步：invalid=True 且 executed_legal=True，两个问题被分开判定"
    ]


def check_budget_exhaustion_is_observable_not_fatal() -> list[str]:
    """预算耗尽必须**可观察**，但**不**是硬失败。

    这两件事必须一起成立。只做前半，会把每一次走到硬停的运行判成失败——
    而硬停是设计好的兜底，不是违规。只做后半，预算耗尽就变成了一个
    谁都看不见的静默事件。
    """
    record = load(f"{BASE}/budget_exhausted.json")
    analysis = analyze(record)
    report = evaluate_rules(analysis)

    _check(analysis.budget_exhausted, "budget_exhausted 数据应当判定为预算耗尽")
    _check(record.state.step_count > record.budget.max_steps, "该数据的步数应超过 max_steps")
    _check(report.passed, f"预算耗尽不应导致 FAIL，实际失败：{[r.rule_id for r in report.gate_failures]}")
    _check(
        "RULE-013" in [r.rule_id for r in report.observations],
        "收尾未收敛到 finish 应作为观察项被记录",
    )
    return ["预算耗尽：可观察（RULE-013）但不 FAIL，且确定性收尾步被识别"]


def check_unavailable_never_reads_as_zero() -> list[str]:
    """不可用的指标不能被当成 0。

    一个把"算不出来"渲染成 0 的报告，会把"不知道"说成"最差"——而且在
    跨运行比较时会让那一项凭空改善或恶化。
    """
    # 分母为 0 时必须 unavailable，而不是 0.0
    _check(rate(0, 0) == UNAVAILABLE, "0/0 应当是 unavailable")
    _check(rate(1, 0) == UNAVAILABLE, "x/0 应当是 unavailable")

    # 求均值时必须跳过 unavailable，而不是把它当 0
    mean, n = mean_with_n([10.0, UNAVAILABLE, 20.0])
    _check(n == 2, f"求均值时应当跳过 unavailable，样本数应为 2，实际 {n}")
    _check(mean == 15.0, f"均值应只由可用的两个值决定，实际 {mean}")

    # 全部不可用时，结果是 unavailable 而不是 0
    all_missing, n = mean_with_n([UNAVAILABLE, UNAVAILABLE])
    _check(all_missing == UNAVAILABLE, f"全部不可用时应返回 unavailable，实际 {all_missing}")
    _check(n == 0, f"全部不可用时样本数应为 0，实际 {n}")

    # 轨迹为空时 invalid_action_count 必须是 unavailable，而不是 0
    empty = _base().model_copy(update={"trace": []})
    from evals.structural.metrics import compute_structural

    metrics = compute_structural(empty)
    _check(
        metrics.invalid_action_count == UNAVAILABLE,
        f"空轨迹的 invalid_action_count 应为 unavailable，实际 {metrics.invalid_action_count!r}",
    )
    _check(
        metrics.invalid_source == METHOD_NONE,
        f"空轨迹的 invalid_source 应为 {METHOD_NONE}，实际 {metrics.invalid_source}",
    )
    return ["unavailable 不参与求均值、不被当成 0、空轨迹上的非法动作计数为 unavailable"]


def check_trend_rules_discriminate() -> list[str]:
    """Phase 5B.1 的三条话题规则必须**两边都能判**。

    这条补的是 `check_every_gate_can_fail` 留下的口子：它只要求每条门禁
    "在违规数据上失败"，于是一条**永远失败**的规则也能过检。而一个永远失败的
    门禁和一个永远通过的门禁一样没用——它会把每一份数据都标红，然后被人关掉。

    所以每一对断言都是一正一反，而且**对照的数据只差一个变量**：

    - RULE-015：多作者话题通过 / 单条爆款被拦（差别在结构，不在分数）；
    - RULE-018：fresh 通过 / stale 被拦（差别**只在时间**，材料与作者完全一致）；
    - RULE-017：带 trend_inputs 的数据通过 / 分数被改动后被拦（差别只在那个数）；
    - RULE-016：正常交付路径通过 / 从未抽取证据被拦。
    """
    notes: list[str] = []
    pairs = (
        ("RULE-015", "multi_author_topic", "single_post_hype"),
        ("RULE-018", "fresh_topic", "stale_topic"),
        ("RULE-016", "healthy_agent_run", "missing_evidence"),
    )
    for rule_id, good, bad in pairs:
        _check(
            _status_of(load(f"{BASE}/{good}.json"), rule_id) == "pass",
            f"{rule_id} 在干净数据 {good} 上应当通过，实际不是",
        )
        _check(
            _status_of(load(f"{BASE}/{bad}.json"), rule_id) == "fail",
            f"{rule_id} 在违规数据 {bad} 上应当失败，实际不是",
        )
        notes.append(f"{rule_id}：{good} 通过 / {bad} 失败")

    good = _base()
    _check(
        _status_of(good, "RULE-017") == "pass",
        "RULE-017 在带 trend_inputs 的 healthy_agent_run 上应当通过，实际不是",
    )
    _check(
        _status_of(
            _with_state(load(f"{BASE}/multi_author_topic.json"), trend_inputs=[]), "RULE-017"
        )
        == "unavailable",
        "RULE-017 在没有 trend_inputs 的数据上应当 unavailable，而不是给出确定结论",
    )
    notes.append("RULE-017：可复算的通过 / 无输入可复算的判为 unavailable")

    # fresh 与 stale 这一对是"只差时间"的对照：材料表与作者完全相同。
    fresh = load(f"{BASE}/fresh_topic.json").state.candidate_topics[0]
    stale = load(f"{BASE}/stale_topic.json").state.candidate_topics[0]
    _check(
        fresh.author_count == stale.author_count
        and fresh.discussion_count == stale.discussion_count,
        "fresh_topic 与 stale_topic 应当同结构（这一对的意义就在于只差时间）",
    )
    _check(
        fresh.qualification.value == "qualified" and stale.qualification.value != "stale",
        "这一对的前提是：干净那份判 qualified、违规那份被**错误地**判成非 stale",
    )
    notes.append(
        f"fresh/stale 同结构（{fresh.author_count} 位作者 / {fresh.discussion_count} 条材料），"
        f"差别只在时间"
    )
    return notes


def check_viewpoint_rules_discriminate() -> list[str]:
    """Phase 5B.3 的四条观点门禁必须**两边都能判**。

    与 `check_trend_rules_discriminate` 同一条理由：只要求"在违规数据上失败"
    的话，一条永远失败的规则也能过检，而它会把每份数据都标红、然后被人关掉。

    五份 fixture 共用同一份状态，各自只在一个变量上与 `viewpoint_supported`
    分叉。所以这里的每一条断言都是在问："同一个东西，只改了那一处，
    门禁的结论就翻过来了吗"——翻不过来，说明它读的不是它声称读的那个变量。
    """
    notes: list[str] = []
    pairs = (
        ("RULE-020", "viewpoint_no_reasoning"),
        ("RULE-021", "viewpoint_overclaim"),
        ("RULE-022", "viewpoint_counterpoint_erased"),
        ("RULE-023", "viewpoint_score_drifted"),
    )
    for rule_id, bad in pairs:
        _check(
            _status_of(load(f"{BASE}/viewpoint_supported.json"), rule_id) == "pass",
            f"{rule_id} 在干净数据 viewpoint_supported 上应当通过，实际不是",
        )
        _check(
            _status_of(load(f"{BASE}/{bad}.json"), rule_id) == "fail",
            f"{rule_id} 在违规数据 {bad} 上应当失败，实际不是",
        )
        notes.append(f"{rule_id}：viewpoint_supported 通过 / {bad} 失败")

    # 五份数据真的只差一个变量：证据包、材料清单与观点条数必须逐字相同。
    # 没有这一条，"只差一个变量"就只是一句自述，哪天有人顺手改了对照组的
    # 证据，报告会继续显示四对漂亮的断言，而它们比的东西已经变了。
    supported = load(f"{BASE}/viewpoint_supported.json")
    for _, bad in pairs:
        other = load(f"{BASE}/{bad}.json")
        _check(
            supported.state.evidence_pack == other.state.evidence_pack,
            f"{bad} 的证据包与对照组不一致——这一对不再是「只差一个变量」",
        )
        _check(
            supported.state.viewpoint_inputs == other.state.viewpoint_inputs,
            f"{bad} 的材料清单与对照组不一致",
        )
    notes.append("五份数据的证据包与材料清单逐字相同（adversarial 对照的前提）")

    # 缺原始输入必须判 unavailable，不能判 pass——照 RULE-017 已经立下的规矩：
    # "缺输入时返回 pass 会让这条规则在大多数数据上变成装饰品"。
    _check(
        _status_of(_with_state(supported, viewpoint_inputs=[]), "RULE-023")
        == "unavailable",
        "RULE-023 在没有 viewpoint_inputs 的数据上应当 unavailable，而不是通过",
    )
    # 反过来：观点没带 assessment（Phase 5B.3 之前的运行）同样判不了。
    # 这条钉住的是"拿新尺子去量历史数据"这件事不会发生。
    legacy = _base()
    _check(
        not any(a.assessment is not None for a in legacy.state.angles),
        "healthy_agent_run 应当保留 5B.3 之前的观点（无 assessment）作对照",
    )
    _check(
        _status_of(legacy, "RULE-023") == "unavailable",
        "RULE-023 在 5B.3 之前的数据上应当 unavailable，而不是判它分数不可复算",
    )
    # 同一条口径也要落在另外三条上：没有选定观点时它们是"不适用"，不是"通过"。
    # 用一个没有观点的运行来问——`_with_state` 把观点那一层清空。
    no_viewpoint = _with_state(legacy, angles=[], selected_angle=None)
    for rule_id in ("RULE-020", "RULE-021", "RULE-022"):
        _check(
            _status_of(no_viewpoint, rule_id) == "pass",
            f"{rule_id} 在没有选定观点的运行上应当是不适用（pass），实际不是",
        )
    notes.append(
        "缺原始输入判 unavailable（RULE-023）；无选定观点判不适用（RULE-020~022）"
    )
    return notes


def check_feedback_rule_discriminates() -> list[str]:
    """RULE-024 必须**两边都能判**，而且判的是它声称读的那个变量。

    与 `check_viewpoint_rules_discriminate` 同一条理由：只要求"在违规数据上
    失败"的话，一条永远失败的规则也能过检——而它会把每份数据都标红、然后
    被人关掉。

    两份 fixture 只差一句话：`feedback_causal_language` 把第一条信号的
    statement 从"观察到分布"改写成因果断言，其余每一条信号、每条局限、
    样本量与证据 id 逐字不变。所以"红的那一条"只能归因于那句话本身。
    """
    notes: list[str] = []
    _check(
        _status_of(load(f"{BASE}/feedback_observed.json"), "RULE-024") == "pass",
        "RULE-024 在干净反馈上下文 feedback_observed 上应当通过，实际不是",
    )
    _check(
        _status_of(load(f"{BASE}/feedback_causal_language.json"), "RULE-024") == "fail",
        "RULE-024 在违规数据 feedback_causal_language 上应当失败，实际不是",
    )
    notes.append("RULE-024：feedback_observed 通过 / feedback_causal_language 失败")

    # 两份数据真的只差那一句话：反馈上下文除 signals[0].statement 之外
    # 必须逐字相同。没有这一条，"只差一个变量"就只是一句自述。
    clean = load(f"{BASE}/feedback_observed.json")
    dirty = load(f"{BASE}/feedback_causal_language.json")
    clean_context = clean.state.feedback_context
    dirty_context = dirty.state.feedback_context
    _check(
        clean_context is not None and dirty_context is not None,
        "两份 fixture 都必须带 feedback_context，否则这一对什么都证明不了",
    )
    clean_tail = clean_context.signals[1:]
    dirty_tail = dirty_context.signals[1:]
    _check(
        clean_tail == dirty_tail,
        "两份 fixture 除第一条信号之外还有别的信号不一致——这一对不再是「只差一个变量」",
    )
    _check(
        clean_context.limitations == dirty_context.limitations
        and clean_context.source_snapshot_ids == dirty_context.source_snapshot_ids
        and clean_context.sample_count == dirty_context.sample_count,
        "两份 fixture 的信号条数/局限/快照 id/样本量必须逐字相同",
    )
    _check(
        clean_context.signals[0].statement != dirty_context.signals[0].statement,
        "两份 fixture 的第一条信号居然一字不差——那它们没有差别，红的那条就没有归因",
    )
    notes.append("两份数据的其余反馈字段逐字相同（对抗性对照的前提）")

    # 缺席必须判 pass（不适用），不能判 fail。
    # 这条钉住的是"拿新尺子去量历史数据"这件事不会发生：本次改动之前
    # 每一次真实运行的 checkpoint 里都没有这个字段。
    legacy = _base()
    _check(
        legacy.state.feedback_context is None,
        "healthy_agent_run 应当保留 5B.4 之前的形状（无 feedback_context）作对照",
    )
    _check(
        _status_of(legacy, "RULE-024") == "pass",
        "RULE-024 在没有反馈上下文的运行上应当是不适用（pass），实际不是",
    )
    notes.append("无 feedback_context 判不适用（pass），不把历史数据染红")
    return notes


def check_every_scored_field_is_reproduced() -> list[str]:
    """RULE-023 比对的字段，必须把 `ViewpointScore` 除 claim/reasons 之外的
    每一项都覆盖到。

    **少比一项，就是给那一项留了一条"可以悄悄变"的路。** 这句话不是修辞：
    这一条检查真的抓到过一次——`scope_declared_missing` 算出来了，却只活在
    `ViewpointScore` 里，压根没写进 `Angle`，于是它既到不了 checkpoint，
    也永远不会被复算比对。这条断言会强制那个字段先在 `Angle` 上有位置。

    `claim` 不比：它是输入不是输出。`reasons` 不比：它是给人看的一句话说明，
    措辞变化不该判成"分数不可复现"。
    """
    from app.agent.viewpoint import ViewpointScore
    from evals.behavior.rules import (
        _VIEWPOINT_ANGLE_FIELDS,
        _VIEWPOINT_ASSESSMENT_FIELDS,
    )

    covered = set(_VIEWPOINT_ANGLE_FIELDS) | set(_VIEWPOINT_ASSESSMENT_FIELDS)
    # `ViewpointScore` 是 dataclass 不是 pydantic 模型（打分器不依赖 pydantic，
    # 它要对"state 里长什么样"零依赖）。所以这里按 dataclass 取字段名。
    expected = {f.name for f in dc_fields(ViewpointScore)} - {"claim", "reasons"}
    _check(
        covered == expected,
        f"RULE-023 漏比的字段：{sorted(expected - covered)}；"
        f"比了不存在的字段：{sorted(covered - expected)}",
    )
    _check(
        not (set(_VIEWPOINT_ANGLE_FIELDS) & set(_VIEWPOINT_ASSESSMENT_FIELDS)),
        "同一个字段同时出现在两组里——两组应当是互斥的",
    )

    # 另一侧：这些字段真的落在 `Angle` / `ViewpointAssessment` 上吗。
    # 上面只检查了两份清单与 `ViewpointScore` 对齐，而 `getattr` 是**读得到
    # 不存在的东西**的——清单里写一个不存在的字段名，`getattr(angle, x)` 会
    # 抛异常（这还算好的）；写成 `ViewpointAssessment` 缺的字段则会在
    # `getattr(assessment, x)` 那里炸。所以这里逐个取一次。
    from app.agent.schemas import Angle, ViewpointAssessment

    angle_fields = set(Angle.model_fields)
    assessment_fields = set(ViewpointAssessment.model_fields)
    _check(
        set(_VIEWPOINT_ANGLE_FIELDS) <= angle_fields,
        f"这些字段不在 Angle 上：{sorted(set(_VIEWPOINT_ANGLE_FIELDS) - angle_fields)}",
    )
    _check(
        set(_VIEWPOINT_ASSESSMENT_FIELDS) <= assessment_fields,
        "这些字段不在 ViewpointAssessment 上："
        f"{sorted(set(_VIEWPOINT_ASSESSMENT_FIELDS) - assessment_fields)}",
    )
    return [
        f"RULE-023 覆盖 {len(covered)} 个字段"
        f"（Angle 上 {len(_VIEWPOINT_ANGLE_FIELDS)} 个 / 明细里 "
        f"{len(_VIEWPOINT_ASSESSMENT_FIELDS)} 个），与 ViewpointScore 逐项对齐"
    ]


def check_clean_fixtures_pass() -> list[str]:
    """干净数据必须全绿——否则门禁会因为误伤而被人关掉。"""
    clean = (
        "healthy_agent_run",
        "accepted_candidate",
        "revision_loop",
        "invalid_action_recovery",
        "multi_author_topic",
        "fresh_topic",
        "viewpoint_supported",
        # Phase 5B.4：带上反馈上下文的运行也必须是全绿的——RULE-024 只在
        # 反馈**真的越界**时才响，读到一批干净观察不是违规。
        "feedback_observed",
    )
    for label in clean:
        record = load(f"{BASE}/{label}.json")
        report = evaluate_rules(analyze(record))
        # invalid_action_recovery 是唯一例外：它的运行**流程**是干净的，
        # 但它包含一次被回退的模型决策，RULE-010 会（正确地）拦下它。
        expected_pass = label != "invalid_action_recovery"
        _check(
            report.passed is expected_pass,
            f"{label}: 期望 passed={expected_pass}，实际 {report.passed}，"
            f"失败项 {[r.rule_id for r in report.gate_failures]}",
        )
    return [f"{len(clean)} 份数据（含 1 份预期被 RULE-010 拦下）的门禁结论符合预期"]


def check_live_mode_is_refused() -> list[str]:
    """`--live` 必须被明确拒绝，而不是悄悄跑起来。"""
    from evals.run_benchmark import LiveModeUnsupported, main

    try:
        main(["--live"])
    except LiveModeUnsupported as exc:
        _check("--live" in str(exc), "拒绝信息里应当说明是 --live 被拒绝")
        return ["--live 被明确拒绝（不是静默忽略，也不是假装跑起来）"]
    raise SelfTestFailure("--live 应当抛出 LiveModeUnsupported，却正常返回了")


def check_scorecard_has_no_total() -> list[str]:
    """分数卡不能有总分字段。

    工作单第七节："不要设计单一聚合 AgentScore = 87"。这条约束最容易在
    后续迭代里被好心违反（"就加一个总分行嘛"），所以用机器守住。
    """
    from evals.scorecard import AgentRunEvaluation

    forbidden = {"score", "total", "total_score", "overall", "overall_score", "grade"}
    present = forbidden & set(AgentRunEvaluation.model_fields)
    _check(not present, f"分数卡里出现了总分字段：{sorted(present)}")
    return ["分数卡里没有 score / total / overall 之类的总分字段"]


def check_chinese_topic_cases_line_up() -> list[str]:
    """中文题评价样例必须与生产的结构契约对得上。

    这份样例回答的是"一条帖子该怎么被评"，而"有哪几条路、有哪几种开头"
    归 `app/agent/post_structure.py` 管。样例里引用一个已经不存在的角色或
    开头形式，**不会**报错——它只会悄悄变成一份过时的参考标准，而参考标准
    过时是最难发现的一种坏：它还在被人照着用。

    依赖方向也在这里被验一次：样例 import `app`，`app` 不 import 样例。
    """
    from app.agent.post_structure import ROLE_ORDER, OpeningMove
    from evals.content.topics import CASES, CRITERIA

    _check(len(CRITERIA) == 8, f"参考判据应当是八条（工作单第六节），实际 {len(CRITERIA)}")
    _check(
        len({c.question for c in CRITERIA}) == len(CRITERIA),
        "参考判据里有重复的判定问题",
    )
    for criterion in CRITERIA:
        _check(bool(criterion.fail_signal.strip()), f"判据「{criterion.question}」没写什么算不合格")

    _check(len(CASES) == 6, f"固定选题应当是六个类别，实际 {len(CASES)}")
    _check(len({c.case_id for c in CASES}) == len(CASES), "选题样例里有重复的 case_id")
    _check(len({c.category for c in CASES}) == len(CASES), "选题样例里有重复的类别名")

    for case in CASES:
        _check(bool(case.goal.strip()), f"{case.case_id} 没有固定选题")
        _check(bool(case.risks), f"{case.case_id} 没写这一类特有的失手方式")
        for role in case.fit_roles:
            _check(role in ROLE_ORDER, f"{case.case_id} 引用了不存在的候选角色 {role}")
        for opening in case.fit_openings:
            _check(
                opening in tuple(OpeningMove),
                f"{case.case_id} 引用了不存在的开头形式 {opening}",
            )

    return [f"六个中文选题类别、八条参考判据，与 post_structure 的角色/开头集合一致"]


CHECKS: tuple[tuple[str, Any], ...] = (
    ("生产的固定措辞能被解析", check_marker_regexes_match_production),
    ("allowed= 的缺失/空/正常三种情况被区分", check_allowed_parsing_distinguishes_missing_and_empty),
    ("每条硬门禁都能在违规数据上失败", check_every_gate_can_fail),
    ("GATE_RULE_IDS 与规则的 severity 一致", check_gate_list_matches_rule_severities),
    ("非法动作不由 allowed= 推出", check_invalid_is_not_derived_from_allowed),
    ("state 快照重放这条路可达", check_replay_path_is_exercised),
    ("预算耗尽可观察但不是硬失败", check_budget_exhaustion_is_observable_not_fatal),
    ("unavailable 不会被读成 0", check_unavailable_never_reads_as_zero),
    ("干净数据全绿", check_clean_fixtures_pass),
    ("话题规则两边都能判", check_trend_rules_discriminate),
    ("观点规则两边都能判", check_viewpoint_rules_discriminate),
    ("反馈规则两边都能判", check_feedback_rule_discriminates),
    ("观点分逐项都可复算", check_every_scored_field_is_reproduced),
    ("中文题评价样例与生产契约没有漂开", check_chinese_topic_cases_line_up),
    ("--live 被拒绝", check_live_mode_is_refused),
    ("分数卡没有总分", check_scorecard_has_no_total),
)


def run_selftest(*, verbose: bool = False, as_json: bool = False) -> int:
    """跑全部自检。返回进程退出码：0 = 全过。"""
    results: list[dict] = []
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
        except Exception as exc:
            failures += 1
            results.append(
                {
                    "check": name,
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if not as_json:
                print(f"✗ {name}")
                print(f"    {type(exc).__name__}: {exc}")

    if as_json:
        print(
            json.dumps(
                {
                    "selftest": "agent-evaluation-lab",
                    "checks": results,
                    "passed": len(results) - failures,
                    "failed": failures,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print()
        print(f"自检：{len(results) - failures} 通过 / {failures} 失败（共 {len(results)} 项）")
        if failures:
            print("评估器本身有问题——在修好之前，任何评估结论都不可信。")

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评估系统的自检")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return run_selftest(verbose=args.verbose, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
