"""比较两份评估结果。

    python evals/compare.py before.json after.json

## 它回答的问题

「这次改动让 Agent 变好了还是变差了」——这是整个 lab 存在的理由。
所以输出按**重要性**分三层，而不是按数据结构的顺序：

1. **门禁变化**——一条从 pass 变成 fail 的门禁是回归，必须排在最前面。
   它是唯一一种"必须马上看"的变化。
2. **可用性变化**——某个指标从"算不出来"变成"算得出来"（或反过来）。
   这一类最容易被忽略，但两者都意味着**标尺变了**：这次比较里前后两个数
   本来就不同源，直接比它们的数值是没有意义的。
3. **指标变化**——同一量纲上的数值差异。这里只列事实，不判好坏：
   `total_steps` 增大在有的场景是"更啰嗦"，在有的场景是"更充分"。

## 为什么不做「综合分变化 = +3.2」

同 `evals/scorecard.py` 拒绝给总分的理由：五个维度量纲不同，合成一个数
会把"安全门禁失败"和"步数多了两步"变成可以互相抵消的东西。比较器如果
打印一个总分差值，那份克制就白费了。

## 退出码

有门禁回归时返回 1（可以拿去当 CI 的回归判据），否则 0。
指标变化**不**影响退出码——那是给人看的，不是门槛。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evals._bootstrap import ensure_repo_root_on_path, force_utf8_stdout

ensure_repo_root_on_path()
force_utf8_stdout()

from evals.values import is_available, pretty  # noqa: E402


class CompareError(RuntimeError):
    """两份结果无法比较（形状不符、缺字段等）。"""


def _load(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise CompareError(f"文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompareError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise CompareError(f"{path} 的顶层必须是对象")
    return data


def _index_cases(data: dict) -> dict[str, dict]:
    """把结果里的 case 按「基准 ID / fixture 名」索引。

    两种模式产出的 case 形状不同（dataset 有 id、fixture 模式没有），
    取两者的并集做键：优先用基准 id（同一份 fixture 可以出现在多条基准里，
    用 id 才能区分 G-006 和 G-007 的不同期望），退回 fixture/stem。
    """
    cases = data.get("cases")
    if cases is None:
        # 单次评估（AgentRunEvaluation 直接落盘）：包成一条 case，
        # 这样比较器只需要处理一种形状。
        return {
            data.get("label", "single"): {
                "id": data.get("label", "single"),
                "fixture": data.get("label", "single"),
                "goal": (data.get("summary") or {}).get("goal", ""),
                "passed": (data.get("gates") or {}).get("passed"),
                "gate_failures": (data.get("gates") or {}).get("failed", []),
                "observations": (data.get("gates") or {}).get("observations", []),
                "unverified_gates": (data.get("gates") or {}).get("unverified", []),
                "evaluation": data,
            }
        }
    indexed: dict[str, dict] = {}
    for case in cases:
        key = case.get("id") or case.get("fixture") or case.get("label")
        if not key:
            raise CompareError(f"case 缺少 id/fixture/label，无法建立索引：{case!r}")
        indexed[key] = case
    return indexed


def _gate_set(case: dict) -> set[str]:
    """一条 case 的门禁失败集合。取**编号**，见 `GateSummary` 的说明。"""
    return {str(g).split()[0] for g in (case.get("gate_failures") or [])}


#: 五个维度。它们在 JSON 里是 `{metrics, notes, unavailable}` 结构，
#: 与 `structural` 的扁平结构不同，所以要分开处理。
_DIMENSIONS = ("autonomy", "research", "writing", "critic", "safety")


def _metrics_of(case: dict) -> dict[str, Any]:
    """把一条 case 里所有可比数值拉平成 `路径 -> 值`。

    路径形如 `structural.total_steps`、`critic.accept_rate`。

    两类要主动排除：

    - **`notes`**。它是解释文字，不是指标。把散文也拉进比较，会让"有人改了
      一句说明"在报告里显示成一次指标变化——而真正该看的 `delta` 会淹没在
      这种噪声里。
    - **`unavailable`** 列表。它列出的是"算不出来"的项名，与 `metrics` 里那些
      值为 `unavailable` 的项重复；两边都收会让同一处变化报两次。
    """
    evaluation = case.get("evaluation") or {}
    flat: dict[str, Any] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else key, value)
        elif isinstance(node, (int, float, bool, str)):
            flat[prefix] = node

    # structural 是扁平的
    if "structural" in evaluation:
        walk("structural", evaluation["structural"])
    # 维度是包着的，只取 metrics
    for section in _DIMENSIONS:
        metrics = (evaluation.get(section) or {}).get("metrics")
        if isinstance(metrics, dict):
            walk(section, metrics)
    # summary 里的标量（draft_chars 等）
    if "summary" in evaluation:
        walk("summary", evaluation["summary"])
    return flat


def compare(before: dict, after: dict) -> dict:
    """比较两份结果，返回结构化的差异描述。纯函数。"""
    before_cases = _index_cases(before)
    after_cases = _index_cases(after)

    only_before = sorted(set(before_cases) - set(after_cases))
    only_after = sorted(set(after_cases) - set(before_cases))
    shared = sorted(set(before_cases) & set(after_cases))

    regressions: list[dict] = []
    improvements: list[dict] = []
    availability_changes: list[dict] = []
    metric_changes: list[dict] = []

    for key in shared:
        old, new = before_cases[key], after_cases[key]
        old_gates, new_gates = _gate_set(old), _gate_set(new)

        newly_failing = sorted(new_gates - old_gates)
        newly_passing = sorted(old_gates - new_gates)
        if newly_failing:
            regressions.append({"case": key, "gates": newly_failing})
        if newly_passing:
            improvements.append({"case": key, "gates": newly_passing})

        old_metrics, new_metrics = _metrics_of(old), _metrics_of(new)
        for path in sorted(set(old_metrics) & set(new_metrics)):
            a, b = old_metrics[path], new_metrics[path]
            a_ok, b_ok = is_available(a), is_available(b)
            if a_ok != b_ok:
                availability_changes.append(
                    {
                        "case": key,
                        "metric": path,
                        "before": pretty(a),
                        "after": pretty(b),
                        "note": "标尺变了：一侧可算、另一侧不可算，数值不可直接比较",
                    }
                )
                continue
            if not a_ok and not b_ok:
                continue
            if isinstance(a, bool) or isinstance(b, bool):
                if a != b:
                    metric_changes.append(
                        {"case": key, "metric": path, "before": pretty(a), "after": pretty(b)}
                    )
                continue
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a != b:
                metric_changes.append(
                    {
                        "case": key,
                        "metric": path,
                        "before": pretty(a),
                        "after": pretty(b),
                        "delta": round(float(b) - float(a), 4),
                    }
                )
            elif a != b:
                metric_changes.append(
                    {"case": key, "metric": path, "before": pretty(a), "after": pretty(b)}
                )

    return {
        "before": {"benchmark": before.get("benchmark", ""), "passed": before.get("passed"), "failed": before.get("failed")},
        "after": {"benchmark": after.get("benchmark", ""), "passed": after.get("passed"), "failed": after.get("failed")},
        "cases_only_in_before": only_before,
        "cases_only_in_after": only_after,
        "regressions": regressions,
        "improvements": improvements,
        "availability_changes": availability_changes,
        "metric_changes": metric_changes,
        "has_regression": bool(regressions),
    }


def render(diff: dict) -> str:
    """把差异渲染成文本。顺序即重要性顺序。"""
    out: list[str] = []
    line = "=" * 72
    out.append(line)
    out.append("评估比较")
    out.append(line)

    def headline(side: str) -> str:
        info = diff[side]
        parts = [info.get("benchmark") or "(未命名)"]
        if info.get("passed") is not None:
            parts.append(f"{info['passed']} 通过 / {info['failed']} 未通过")
        return "  ".join(str(p) for p in parts)

    out.append(f"  之前：{headline('before')}")
    out.append(f"  之后：{headline('after')}")
    out.append("")

    if diff["regressions"]:
        out.append(f"⛔ 门禁回归（{len(diff['regressions'])} 条）—— 这是要看的部分")
        for item in diff["regressions"]:
            out.append(f"    {item['case']}: 新增门禁失败 {', '.join(item['gates'])}")
        out.append("")
    else:
        out.append("✓ 没有门禁回归")
        out.append("")

    if diff["improvements"]:
        out.append(f"门禁修复（{len(diff['improvements'])} 条）")
        for item in diff["improvements"]:
            out.append(f"    {item['case']}: 不再失败 {', '.join(item['gates'])}")
        out.append("")

    if diff["cases_only_in_before"]:
        out.append(f"只在『之前』出现的基准：{', '.join(diff['cases_only_in_before'])}")
    if diff["cases_only_in_after"]:
        out.append(f"只在『之后』出现的基准：{', '.join(diff['cases_only_in_after'])}")
    if diff["cases_only_in_before"] or diff["cases_only_in_after"]:
        out.append("")

    if diff["availability_changes"]:
        out.append(
            f"可用性变化（{len(diff['availability_changes'])} 处）"
            f"—— 标尺变了，这些数前后不可直接比"
        )
        for item in diff["availability_changes"]:
            out.append(
                f"    {item['case']} {item['metric']}: "
                f"{item['before']} → {item['after']}"
            )
        out.append("")

    if diff["metric_changes"]:
        out.append(
            f"指标变化（{len(diff['metric_changes'])} 处）"
            f"—— 只列事实，不判好坏"
        )
        for item in diff["metric_changes"]:
            delta = item.get("delta")
            arrow = f"{item['before']} → {item['after']}"
            if isinstance(delta, (int, float)):
                arrow += f"  ({delta:+g})"
            out.append(f"    {item['case']} {item['metric']}: {arrow}")
        out.append("")

    out.append(line)
    out.append("结论：有门禁回归" if diff["has_regression"] else "结论：无名禁回归")
    out.append(line)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/compare.py",
        description="比较两份评估结果（由 run_benchmark.py --out 产生）",
    )
    parser.add_argument("before", help="基线结果 JSON")
    parser.add_argument("after", help="新结果 JSON")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    args = parser.parse_args(argv)

    diff = compare(_load(args.before), _load(args.after))
    if args.json:
        print(json.dumps(diff, ensure_ascii=False, indent=2))
    else:
        print(render(diff))
    return 1 if diff["has_regression"] else 0


if __name__ == "__main__":
    sys.exit(main())
