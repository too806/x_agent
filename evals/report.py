"""把评估结果渲染成人看的文本。

## 这个模块唯一要守住的东西：不输出总分

`evals/scorecard.py` 拒绝算一个 `AgentScore = 87`，理由是那个数会把五个
量纲不同的维度（自主性、研究、写作、评审、安全）压成一个既不可解释、
也不可回滚的排名。渲染层如果自作主张打印一个平均分，那份克制就白费了。

所以这里**只**做三件事：

1. 把每个维度自己的指标**带来源**地列出来；不可用的写 `unavailable`，
   绝不渲染成 0（那是"最差"，不是"不知道"）。
2. 把门禁结论说清楚，并且**分开**印"失败"和"未验证"——一条没跑起来的
   门禁和一条通过的门禁，在"这次运行可信吗"这个问题上答案完全不同。
3. 把合成数据的来源与 note 原样带出来，避免一份 fixture 的报告被人当成实测。

## 宽度与对齐

不引第三方表格库：评估报告的读者是终端里的人，而列宽固定的表格在
中文 + 数字混排时会错位。这里用"标签 + 值 + 来源"的短行，不用表格。
"""

from __future__ import annotations

from evals.scorecard import AgentRunEvaluation, Dimension
from evals.values import UNAVAILABLE, is_available, pretty

#: 报告默认印出的维度顺序。与 `AgentRunEvaluation` 的字段顺序一致，
#: 但显式写出来，这样"报告少印了一个维度"是可以被看出来的一件事。
DIMENSION_ORDER: tuple[tuple[str, str], ...] = (
    ("autonomy", "自主性"),
    ("research", "研究"),
    ("writing", "写作"),
    ("critic", "评审"),
    ("safety", "安全"),
)

RULE_EXPLANATION_WIDTH = 78


def _shorten(text: str, width: int = RULE_EXPLANATION_WIDTH) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def render_dimension(name: str, dimension: Dimension) -> list[str]:
    """一个维度的若干行。"""
    lines = [f"  {name}"]
    if not dimension.metrics:
        lines.append("    （无指标）")
        return lines

    for key, value in dimension.metrics.items():
        note = dimension.notes.get(key)
        rendered = pretty(value)
        if note:
            # 不可用的指标**必须**带解释，否则读者只看到一个 unavailable，
            # 没法判断是"数据不够"还是"这一项本来就不适用"。
            lines.append(f"    {key:32s} {rendered:>12s}   ({note})")
        else:
            lines.append(f"    {key:32s} {rendered:>12s}")

    if dimension.unavailable:
        lines.append(
            f"    不可用：{', '.join(dimension.unavailable)}"
            f" —— 这些项在这次数据上无法计算，不是 0"
        )
    return lines


def render_run(evaluation: AgentRunEvaluation, *, verbose: bool = False) -> str:
    """单次评估的完整文本报告。"""
    out: list[str] = []
    line = "=" * 72

    out.append(line)
    out.append(f"运行：{evaluation.label}")
    if evaluation.synthetic:
        # 合成数据必须一眼可见。一份 fixture 报告被当成实测，是这套系统
        # 最可能产生的一种误读。
        out.append(
            f"⚠ 合成数据（synthetic={evaluation.synthetic}，"
            f"provenance={evaluation.provenance}）—— 不是实测结果"
        )
    if evaluation.note:
        out.append(f"说明：{_shorten(evaluation.note)}")
    out.append(line)

    summary = evaluation.summary
    if summary.goal:
        out.append(f"目标：{_shorten(summary.goal)}")
    if summary.selected_topic:
        out.append(f"选题：{_shorten(summary.selected_topic)}")
    if summary.draft_preview:
        out.append(f"草稿：{summary.draft_chars} 字 | {_shorten(summary.draft_preview)}")
    if evaluation.budget:
        out.append(
            "预算："
            + "，".join(f"{k}={v}" for k, v in evaluation.budget.items())
        )
    out.append("")

    out.append("结构指标")
    for key, value in evaluation.structural.model_dump().items():
        if key.endswith("_source"):
            continue
        out.append(f"    {key:32s} {pretty(value):>12s}")
    out.append(f"    {'invalid_source':32s} {evaluation.structural.invalid_source:>12s}")
    out.append(f"    {'allowed_source':32s} {evaluation.structural.allowed_source:>12s}")
    out.append("")

    out.append("维度（分数卡：各维度独立，刻意不合并成总分）")
    for field, label in DIMENSION_ORDER:
        out.extend(render_dimension(label, getattr(evaluation, field)))
        out.append("")

    gates = evaluation.gates
    titles = {r.get("rule_id"): r.get("title", "") for r in evaluation.rules}

    def labelled(rule_id: str) -> str:
        """`RULE-010` → `RULE-010 标题`。门禁列表里存的是编号（见 GateSummary）。"""
        title = titles.get(rule_id)
        return f"{rule_id} {title}" if title else rule_id

    out.append("回归门禁")
    out.append(f"    结论：{'通过' if gates.passed else '未通过'}")
    if gates.failed:
        out.append(
            "    失败（这些让整次评估 FAIL）："
            + "；".join(labelled(r) for r in gates.failed)
        )
    if gates.unverified:
        out.append(
            "    未验证（不导致 FAIL，但也**不**等于通过）："
            + "；".join(labelled(r) for r in gates.unverified)
        )
    if gates.observations:
        out.append(
            "    观察项（不导致 FAIL）："
            + "；".join(labelled(r) for r in gates.observations)
        )
    out.append("")

    problems = [r for r in evaluation.rules if r.get("status") != "pass"]
    if problems:
        out.append("规则明细（只列非 pass 的；全部判定见 JSON）")
        for rule in problems:
            mark = {"fail": "✗", "unavailable": "?"}.get(rule.get("status", ""), "·")
            out.append(
                f"    {mark} {rule.get('rule_id', '?'):10s} "
                f"[{rule.get('severity', '?')}] {_shorten(rule.get('title', ''), 40)}"
            )
            out.append(f"        {_shorten(rule.get('detail', ''))}")
            for evidence in (rule.get("evidence") or [])[:4]:
                out.append(f"        - {_shorten(str(evidence))}")
        out.append("")
    elif verbose:
        out.append("规则明细：全部通过")
        out.append("")

    out.append(line)
    out.append(f"结论：{evaluation.verdict}")
    out.append(line)
    return "\n".join(out)


def render_benchmark(result: dict, *, verbose: bool = False) -> str:
    """整套 benchmark 的文本报告。"""
    out: list[str] = []
    line = "=" * 72
    out.append(line)
    out.append(f"Benchmark：{result.get('benchmark', '(未命名)')}")
    if result.get("synthetic"):
        out.append("⚠ 全部为合成数据（synthetic fixtures）—— 不是实测结果")
    out.append(line)
    out.append("")

    for case in result.get("cases", []):
        mark = "✓" if case.get("passed") else "✗"
        status = "通过" if case.get("passed") else "未通过"
        out.append(
            f"{mark} {case.get('id', '?'):8s} {status:4s} "
            f"{case.get('fixture', case.get('label', ''))}"
        )
        out.append(f"    {_shorten(case.get('goal', ''))}")
        if case.get("gate_failures"):
            out.append(f"    门禁失败：{', '.join(case['gate_failures'])}")
        if case.get("observations"):
            out.append(f"    观察项：{', '.join(case['observations'])}")
        if case.get("unverified_gates"):
            out.append(f"    未验证：{', '.join(case['unverified_gates'])}")
        if case.get("mismatch"):
            out.append(f"    ⚠ 与数据集期望不符：{_shorten(case['mismatch'])}")
        out.append("")

    out.append(line)
    passed = result.get("passed", 0)
    failed = result.get("failed", 0)
    out.append(f"合计：{passed} 通过 / {failed} 未通过（共 {passed + failed} 条）")
    out.append(line)

    metrics = result.get("metrics") or {}
    if metrics:
        out.append("")
        out.append("聚合指标（仅记录，不参与判定）")
        for key, value in metrics.items():
            suffix = "" if is_available(value) else "  ← 无法计算，不是 0"
            out.append(f"    {key:36s} {pretty(value):>12s}{suffix}")

    if verbose:
        out.append("")
        out.append("逐条明细")
        for case in result.get("cases", []):
            evaluation = case.get("evaluation")
            if evaluation:
                out.append(render_run(AgentRunEvaluation(**evaluation), verbose=True))
                out.append("")
    return "\n".join(out)


def verdict_line(evaluation: AgentRunEvaluation) -> str:
    """一行的结论，给 `--json` 之外的紧凑输出用。"""
    gates = evaluation.gates
    if not gates.passed:
        return f"{evaluation.label}: FAIL（{', '.join(gates.failed)}）"
    if gates.unverified:
        return (
            f"{evaluation.label}: PASS（未验证：{', '.join(gates.unverified)}）"
        )
    return f"{evaluation.label}: PASS"


__all__ = [
    "DIMENSION_ORDER",
    "render_benchmark",
    "render_dimension",
    "render_run",
    "verdict_line",
    "UNAVAILABLE",
]
