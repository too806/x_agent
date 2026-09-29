"""Agent Evaluation Lab 的入口。

    python evals/run_benchmark.py              # 跑 fixtures/*.json（默认）
    python evals/run_benchmark.py --dataset    # 跑 datasets/goals.json 里的 10 条基准
    python evals/run_benchmark.py --json       # 机器可读输出
    python evals/run_benchmark.py --out r.json # 另存一份结果
    python evals/run_benchmark.py --selftest   # 跑自检
    python evals/run_benchmark.py --live ...   # 明确拒绝

## 为什么默认是 fixture 而不是真实运行

两件事决定了这个选择：

1. **可复现**。真实运行要 PostgreSQL、要 X、要模型，每次结果都不一样；
   那样的评估只能回答"这次跑得怎么样"，回答不了"这次改动有没有让它变差"。
2. **可离线**。CI 上不该为了跑评估去连一个外部服务，那会让评估变成
   最不稳定的一环——而它本该是最稳定的那一环。

所以 lab 的默认输入是 `evals/fixtures/*.json`。真实运行走的是**导出**这条路：
把一次真实 Run 导出成同样的形状，再喂进来（见 `evals/loader.py` 的
`load_export`）。评估器只认形状，不认来源。

## `--live` 为什么直接拒绝

它会让评估偷偷依赖外部服务，从而在"没有 X 的时候"和"X 挂了的时候"给出
两种不同的结论。真要做在线评估，那条路径应当是一个**独立**的入口，
并且把"用了哪个模型、哪次调用"记进结果里；现在没有这个能力，
所以诚实地拒绝，而不是做一个半吊子的假装在线。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evals._bootstrap import ensure_repo_root_on_path, force_utf8_stdout

ensure_repo_root_on_path()
force_utf8_stdout()

from evals import __version__  # noqa: E402
from evals.loader import (  # noqa: E402
    LoadError,
    fixture_paths,
    load,
    load_dataset,
)
from evals.report import render_benchmark, render_run  # noqa: E402
from evals.scorecard import AgentRunEvaluation, evaluate  # noqa: E402
from evals.values import UNAVAILABLE, mean_with_n  # noqa: E402

DEFAULT_DATASET = "goals.json"


class LiveModeUnsupported(RuntimeError):
    """`--live` 的拒绝理由。单独定义是为了让自检能断言它确实会被抛出。"""


def _evaluate_fixture(path: Path) -> AgentRunEvaluation:
    return evaluate(load(path))


def _aggregate(evaluations: list[AgentRunEvaluation]) -> dict:
    """整套评估的聚合指标。

    刻意**不**给一个总分。这里只做两件事：
    - 计数（多少次运行、多少次带了非法动作、多少次进了人工审核…）；
    - 对**同一量纲**的指标求均值。

    第二件事有个容易做错的地方：某条运行算不出某个指标时要返回
    `unavailable`，而不是当成 0 参与求平均——那会把"不知道"变成"很差"。
    `mean_with_n` 负责这件事，它的返回值里带 n。

    去重：`--dataset` 里多条基准可以指向同一份 fixture（G-006 与 G-007 都跑
    budget_exhausted）。不去重的话，那一份数据会被数两次，"10 条运行里 3 条
    预算耗尽"这种数字就悄悄失真了。按 label 去重而不是按文件路径——
    label 是评估结果里唯一的身份，路径不是。
    """
    unique: dict[str, AgentRunEvaluation] = {}
    for evaluation in evaluations:
        unique.setdefault(evaluation.label, evaluation)
    unique_evaluations = list(unique.values())
    structural = [e.structural for e in unique_evaluations]

    def count(predicate) -> int:
        return sum(1 for s in structural if predicate(s))

    def safety_flag(evaluation: AgentRunEvaluation, key: str) -> object:
        return evaluation.safety.metrics.get(key, UNAVAILABLE)

    return {
        "runs": len(unique_evaluations),
        "distinct_fixtures": len(unique),
        "runs_with_invalid_action": count(
            lambda s: isinstance(s.invalid_action_count, int) and s.invalid_action_count > 0
        ),
        # 从 safety 维度取，而不是自己从 finish_reason 猜：人工审核是否到达
        # 是一个有明确来源（run_status / human_action）的事实，猜出来的版本
        # 会在"运行正常结束但没进审核"时给出恰好相反的答案。
        "runs_reaching_human_review": sum(
            1 for e in unique_evaluations if safety_flag(e, "human_review_reached") is True
        ),
        "runs_with_autonomous_publish": sum(
            1 for e in unique_evaluations if safety_flag(e, "autonomous_publish") is True
        ),
        "runs_budget_exhausted": count(lambda s: s.budget_exhausted),
        "runs_hard_exhausted": count(lambda s: s.hard_exhausted),
        "mean_total_steps": mean_with_n([s.total_steps for s in structural])[0],
        "mean_planner_steps": mean_with_n([s.planner_steps for s in structural])[0],
        "mean_research_calls": mean_with_n([s.research_calls for s in structural])[0],
        "mean_revision_count": mean_with_n([s.revision_count for s in structural])[0],
        "total_unknown_actions": sum(s.unknown_action_count for s in structural),
        "runs_failing_gates": sum(
            1 for e in unique_evaluations if not e.gates.passed
        ),
        "unverified_gates": sum(
            len(e.gates.unverified) for e in unique_evaluations
        ),
        "observations": sum(
            len(e.gates.observations) for e in unique_evaluations
        ),
    }


def _case_from_expectation(
    goal_entry: dict, evaluation: AgentRunEvaluation
) -> dict:
    """把一条基准目标与一次评估结果对起来，判断是否与期望相符。

    期望**只**关于门禁结论（§14）。指标不参与判定——那段说明在
    `datasets/goals.json` 的 `how_to_read` 里，这里是它的执行。
    """
    expect = goal_entry.get("expect", {})
    gates = evaluation.gates

    mismatch: list[str] = []
    expected_passed = expect.get("passed")
    if expected_passed is not None and gates.passed != expected_passed:
        mismatch.append(
            f"期望 passed={expected_passed}，实际 {gates.passed}"
        )
    expected_gates = expect.get("gate_failures")
    if expected_gates is not None and sorted(gates.failed) != sorted(expected_gates):
        mismatch.append(
            f"期望门禁失败 {expected_gates}，实际 {gates.failed}"
        )
    expected_obs = expect.get("observations")
    actual_obs = [o for o in gates.observations]
    if expected_obs is not None and sorted(actual_obs) != sorted(expected_obs):
        mismatch.append(f"期望观察项 {expected_obs}，实际 {actual_obs}")

    return {
        "id": goal_entry.get("id", ""),
        "goal": goal_entry.get("goal", ""),
        "question": goal_entry.get("question", ""),
        "fixture": goal_entry.get("fixture", ""),
        "label": evaluation.label,
        "passed": not mismatch,
        "gate_failures": gates.failed,
        "observations": gates.observations,
        "unverified_gates": gates.unverified,
        "mismatch": "；".join(mismatch),
        "evaluation": evaluation.model_dump(mode="json"),
    }


def run_fixtures(*, verbose: bool = False) -> dict:
    """默认模式：把 `evals/fixtures/` 下的每份数据当一条独立的评估跑。

    这一模式**不**判定通过与否——fixture 里有故意造出来的缺陷数据，
    它们"失败"才是正确的。它输出的是每条运行的分数卡。
    """
    paths = fixture_paths()
    if not paths:
        raise LoadError("evals/fixtures/ 下没有任何 fixture")

    evaluations: list[AgentRunEvaluation] = []
    cases: list[dict] = []
    for path in paths:
        evaluation = _evaluate_fixture(path)
        evaluations.append(evaluation)
        cases.append(
            {
                "id": path.stem,
                "goal": evaluation.summary.goal,
                "fixture": path.stem,
                "label": evaluation.label,
                "passed": evaluation.gates.passed,
                "gate_failures": evaluation.gates.failed,
                "observations": evaluation.gates.observations,
                "unverified_gates": evaluation.gates.unverified,
                "mismatch": "",
                "evaluation": evaluation.model_dump(mode="json"),
            }
        )

    return {
        "benchmark": "fixtures",
        "evals_version": __version__,
        "synthetic": True,
        "mode": "fixture",
        "cases": cases,
        # fixture 模式下 passed/failed 描述的是"这份数据里的运行有没有触发硬门禁"，
        # 不是"lab 自测通过率"。真正该全绿的是 --selftest。
        "passed": sum(1 for e in evaluations if e.gates.passed),
        "failed": sum(1 for e in evaluations if not e.gates.passed),
        "metrics": _aggregate(evaluations),
    }


def run_dataset(name: str = DEFAULT_DATASET) -> dict:
    """基准模式：按 `datasets/goals.json` 的期望逐条判定。"""
    dataset = load_dataset(name)
    entries = dataset.get("goals") or []
    if not entries:
        raise LoadError(f"{name} 里没有 goals")

    cases: list[dict] = []
    evaluations: list[AgentRunEvaluation] = []
    for entry in entries:
        fixture = entry.get("fixture")
        if not fixture:
            raise LoadError(f"基准 {entry.get('id')} 没有指定 fixture")
        evaluation = _evaluate_fixture(
            Path(__file__).parent / "fixtures" / f"{fixture}.json"
        )
        evaluations.append(evaluation)
        cases.append(_case_from_expectation(entry, evaluation))

    return {
        "benchmark": dataset.get("name", name),
        "evals_version": __version__,
        "synthetic": True,
        "mode": "dataset",
        "cases": cases,
        "passed": sum(1 for c in cases if c["passed"]),
        "failed": sum(1 for c in cases if not c["passed"]),
        "metrics": _aggregate(evaluations),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evals/run_benchmark.py",
        description="Agent Evaluation Lab —— 离线、可复现的 Agent 评估",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fixture",
        action="store_true",
        help="跑 evals/fixtures/*.json（默认）",
    )
    mode.add_argument(
        "--dataset",
        nargs="?",
        const=DEFAULT_DATASET,
        default=None,
        metavar="NAME",
        help=f"按数据集里的期望判定（默认 {DEFAULT_DATASET}）",
    )
    mode.add_argument(
        "--selftest",
        action="store_true",
        help="跑 lab 自己的自检：断言每条回归门禁都会触发",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="（不支持）对真实环境跑评估",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    parser.add_argument("--out", metavar="PATH", help="把 JSON 结果另存到文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印逐条明细")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.live:
        raise LiveModeUnsupported(
            "--live 不受支持：本 lab 刻意只做离线评估。"
            "在线评估会让结论依赖外部服务是否可达，"
            "从而在『没有 X』和『X 挂了』两种情况下给出不同的结果。"
            "要评估真实运行，请把它导出成 evals/loader.py 认识的形状"
            "（run + decisions + checkpoint_state + review + publish），再喂给 --fixture。"
        )

    if args.selftest:
        from evals.selftest import run_selftest

        return run_selftest(verbose=args.verbose, as_json=args.json)

    result = run_dataset(args.dataset) if args.dataset else run_fixtures()

    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写入 {args.out}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("mode") == "dataset":
        print(render_benchmark(result, verbose=args.verbose))
    else:
        for evaluation in result["cases"]:
            print(render_run(AgentRunEvaluation(**evaluation["evaluation"]), verbose=args.verbose))
            print()

    # 退出码：dataset 模式下"与期望不符"就是失败；fixture 模式下
    # 有 fixture 触发硬门禁是**预期**的，不能当失败——否则这个入口
    # 在仓库的默认数据上永远返回非零，谁都不会再看它。
    if result.get("mode") == "dataset":
        return 1 if result["failed"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
