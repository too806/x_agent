"""把磁盘上的 JSON 读成 `AgentRunRecord`。

支持三种输入形状，因为它们对应三种真实来源：

1. **fixture**（`evals/fixtures/*.json`）——合成场景，字段齐全、显式声明预算。
2. **live export**——从真实运行导出的三条数据拼起来：
   `agent_run` 行 + `agent_decision` 行 + checkpoint 快照。
   形状见 `load_export()`，导出片段的取法见 docs/agent-evaluation/metrics.md。
3. **裸 state**——只有一个 checkpoint `RunState` 字典，trace 从
   `state["decision_trace"]` 还原。这是最小可用的真实数据：一个人从
   `graph.aget_state(config)` 里 `print(snapshot.values)` 就能得到。

三种形状最后都收敛到同一个 `AgentRunRecord`。**下游评估器只认这一种类型**，
所以"支持什么输入"是一个集中在 `load()` 里的问题，不会渗进指标代码。

## 不读数据库

刻意不提供直连 PostgreSQL 的加载器。原因不是懒：评估的核心用途是
**Version A trace vs Version B trace 的离线比较**，而数据库里只有"当前"那一版。
一旦评估器依赖库，历史 trace 就取不回来，比较也就做不成。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agent.budget import AgentBudget, load_budget
from evals.model import AgentRunRecord, AgentRunState, DraftInfo, RunOutcome, TraceStep

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
DATASET_DIR = Path(__file__).resolve().parent / "datasets"


class LoadError(RuntimeError):
    """输入不合法，或形状无法识别。

    刻意不降级成"用默认值继续跑"：一份读不懂的 trace 若能被评估出结论，
    那个结论就是编的。评估工具最不能做的事就是对自己的输入睁一只眼。
    """


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LoadError(f"找不到文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise LoadError(f"{path} 不是合法 JSON：{exc}") from exc


def resolve_budget(data: dict[str, Any]) -> AgentBudget:
    """取这次运行当时用的预算。

    未显式声明时回退到 `config/agent.yaml` 的**当前**值。这个回退有已知偏差
    （`app/agent/metrics.py` 的 docstring 记录过同一个坑：预算被改小之后，
    历史 Run 的 `budget_exhausted` 会跟着变），所以：
    fixture 一律显式声明；回退路径只服务临时导出的真实数据。
    """
    declared = data.get("budget")
    if declared:
        return AgentBudget(**declared)
    return load_budget()


def _derive_outcome(raw: dict[str, Any], state: AgentRunState) -> RunOutcome:
    """把零散字段收成一个 RunOutcome，并补上可推导的部分。

    `human_review_reached` 单独推导是必要的：真实导出里常常只有 `run.status`，
    而它是 `waiting_for_review` 还是 `approved` 取决于人有没有点按钮——
    两者都**已经**进了人工审核。只看 `human_action` 会把"人还没审"误判成
    "没进入审核"，那正好是 RULE-008 要抓的反面。
    """
    outcome = RunOutcome(**{k: v for k, v in (raw or {}).items() if k in RunOutcome.model_fields})

    if outcome.human_review_reached is None:
        reviewed_states = {"waiting_for_review", "approved", "rejected", "published"}
        human_action = outcome.human_action or state.human_action
        outcome.human_review_reached = bool(
            outcome.run_status in reviewed_states or human_action
        )
    if outcome.human_action is None and state.human_action in ("approve", "edit", "reject"):
        outcome.human_action = state.human_action  # type: ignore[assignment]
    return outcome


def _derive_draft(raw: dict[str, Any], state: AgentRunState) -> DraftInfo:
    draft = DraftInfo(**{k: v for k, v in (raw or {}).items() if k in DraftInfo.model_fields})
    if not draft.content:
        # final_content 优先：edit 路径下人工改写稿才是真正被审核/发布的东西。
        draft.content = state.final_content or state.draft_content
    if not draft.draft_id:
        draft.draft_id = state.draft_id
    return draft


def _derive_exit_code_trace(state: AgentRunState) -> list[TraceStep]:
    """从 state.decision_trace 还原轨迹（裸 state 输入时用）。

    `DecisionTraceEntry` 与 `TraceStep` 的字段几乎一一对应，唯一的差别是
    state 条目没有 `input_summary`——那正是 `AgentRunRecord.steps` 想补的东西，
    补不上就留空，并在报告里标注 `trace_source=state`。
    """
    raw = (state.model_extra or {}).get("decision_trace")
    if not raw:
        return []
    # 裸 state 可能来自 JSON（dict），也可能来自内存里的 pydantic 对象
    # （`DecisionTraceEntry`）。两种都要能读——否则"评估真实 trace"
    # 就变成了"评估真实 trace 的 JSON 副本"，多一道手工转换就多一处走样。
    return [TraceStep(**entry if isinstance(entry, dict) else entry.model_dump()) for entry in raw]


def load_fixture(path: Path) -> AgentRunRecord:
    """读一个 fixture：字段齐全的合成场景。"""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise LoadError(f"{path} 顶层必须是 JSON 对象")

    state = AgentRunState(**data.get("state", {}))
    trace = [TraceStep(**step) for step in data.get("trace", [])] or _derive_exit_code_trace(state)
    try:
        record = AgentRunRecord(
            label=data.get("label") or path.stem,
            synthetic=bool(data.get("synthetic", True)),
            provenance=data.get("provenance", "fixture"),
            note=data.get("note", ""),
            run_id=data.get("run_id", state.run_id),
            goal=data.get("goal", state.goal),
            budget=resolve_budget(data),
            state=state,
            trace=trace,
            outcome=_derive_outcome(data.get("outcome", {}), state),
            draft=_derive_draft(data.get("draft", {}), state),
            steps=data.get("steps"),
            content_quality=data.get("content_quality", {}),
        )
    except ValidationError as exc:
        # 生产 schema 变了而 fixture 没跟上——必须炸，不能默认值糊过去。
        raise LoadError(f"{path} 与当前 Agent schema 不符：{exc}") from exc
    return record


def load_export(path: Path) -> AgentRunRecord:
    """读一份真实运行导出。

    期望形状（缺项用默认值，因此可以只给有的那些）::

        {
          "run":       {"id": "...", "goal": "...", "status": "waiting_for_review",
                        "error": ""},
          "decisions": [{"step": 1, "action": "discover_topics", "reason": "...",
                         "target": null, "input_summary": "...",
                         "result_summary": "..."}],
          "checkpoint_state": { ...RunState... },
          "review":    {"action": "approve", "edited_content": null},
          "publish":   {"published": false}
        }

    `run` / `checkpoint_state` 对应 `agent_run` 表与 `aget_state()` 的 values，
    `decisions` 对应 `agent_decision` 表按 step 排序的行。三者都来自现有系统，
    没有为本评估工具新增任何日志。
    """
    data = _read_json(path)
    if not isinstance(data, dict):
        raise LoadError(f"{path} 顶层必须是 JSON 对象")

    run = data.get("run") or {}
    state = AgentRunState(**(data.get("checkpoint_state") or {}))
    if not state.run_id:
        state = state.model_copy(update={"run_id": run.get("id", "")})

    decisions = data.get("decisions") or []
    trace = [TraceStep(**step) for step in decisions] or _derive_exit_code_trace(state)

    review = data.get("review") or {}
    publish = data.get("publish") or {}
    outcome_raw: dict[str, Any] = {
        "run_status": run.get("status", ""),
        "error": run.get("error", ""),
        "human_action": review.get("action"),
        "published": publish.get("published"),
        "publish_initiated_by": publish.get("initiated_by", "none"),
    }

    draft_raw: dict[str, Any] = {"human_edited_content": review.get("edited_content")}
    try:
        return AgentRunRecord(
            label=data.get("label") or path.stem,
            synthetic=False,
            provenance="live-export",
            note=data.get("note", ""),
            run_id=run.get("id", state.run_id),
            goal=run.get("goal") or state.goal,
            budget=resolve_budget(data),
            state=state,
            trace=trace,
            outcome=_derive_outcome(outcome_raw, state),
            draft=_derive_draft(draft_raw, state),
            steps=data.get("steps"),
            content_quality=data.get("content_quality", {}),
        )
    except ValidationError as exc:
        raise LoadError(f"{path} 与当前 Agent schema 不符：{exc}") from exc


def from_state(state: dict[str, Any], *, label: str = "inline-state", **kwargs: Any) -> AgentRunRecord:
    """从一份**裸 checkpoint state**（或其 JSON 序列化）建记录。

    这是最省事的真实数据入口：不需要建库、不需要跑 Agent，
    把 `snapshot.values` 落成 JSON 就能评估。代价是拿不到 `input_summary`，
    且 outcome 只能从 state 自身推导（拿不到 `agent_run.status`）。
    """
    parsed = AgentRunState(**state)
    return AgentRunRecord(
        label=label,
        synthetic=False,
        provenance="recorded",
        run_id=parsed.run_id,
        goal=parsed.goal,
        budget=kwargs.pop("budget", None) or load_budget(),
        state=parsed,
        trace=_derive_exit_code_trace(parsed),
        outcome=_derive_outcome(kwargs.pop("outcome", {}) or {}, parsed),
        draft=_derive_draft(kwargs.pop("draft", {}) or {}, parsed),
        **kwargs,
    )


def load(path: str | Path) -> AgentRunRecord:
    """自动识别形状并加载。三种形状的判别只看顶层键，不看内容。"""
    path = Path(path)
    data = _read_json(path)
    if not isinstance(data, dict):
        raise LoadError(f"{path} 顶层必须是 JSON 对象")

    if "checkpoint_state" in data or "decisions" in data or "run" in data:
        return load_export(path)
    if "state" in data:
        return load_fixture(path)
    raise LoadError(
        f"{path} 形状无法识别：既没有 fixture 的 'state'，"
        f"也没有导出格式的 'run' / 'decisions' / 'checkpoint_state'。"
        f"顶层键为 {sorted(data)}"
    )


def fixture_paths() -> list[Path]:
    """全部 fixture，按文件名排序（保证 benchmark 可复现）。"""
    return sorted(FIXTURE_DIR.glob("*.json"))


def load_all_fixtures() -> list[AgentRunRecord]:
    return [load(p) for p in fixture_paths()]


def load_dataset(name: str = "goals.json") -> dict[str, Any]:
    """读 benchmark 数据集。"""
    data = _read_json(DATASET_DIR / name)
    if not isinstance(data, dict) or "goals" not in data:
        raise LoadError(f"{DATASET_DIR / name} 顶层必须是含 'goals' 的对象")
    return data
