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


def check_clean_fixtures_pass() -> list[str]:
    """干净数据必须全绿——否则门禁会因为误伤而被人关掉。"""
    clean = ("healthy_agent_run", "accepted_candidate", "revision_loop", "invalid_action_recovery")
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
