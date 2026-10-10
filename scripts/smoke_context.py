"""Context / Background 的真机验收（工作单第十九节）。

跑**一次**真实的 Agent 运行（真实 X 检索 + 真实 LLM），目标就是那条暴露问题的
formulation：`今天关注美股，找一个能讨论的主题`。要看的不是"稿子写得好不好"，
而是这条链路真的被接上了没有：

| 观察面 | 判据 |
|---|---|
| Evidence 是否产出背景 | `evidence_pack.context` 非空，四段里至少有一段有内容 |
| 边界守卫是否生效 | `origin` 是三者之一；`fact` 的出处必须落在本次材料 id 上 |
| Writer 是否拿到背景 | `candidates.txt` 的 `{{CONTEXT}}` 被替换成了真实内容（从 prompt 侧无法事后看，这里只用产出侧间接说明） |
| Critic 是否判了 `context_fit` | 至少一条候选带回四项里的若干项 |
| 真机那份故障形态有没有被拦 | 首句"把一条事实当入口"的确定性信号命中情况 |

## 绝不发布

`RealXApiClient._request` 被换成同一套只读守卫：**非 GET 在发出去之前就抛**，
每个请求留痕，报告末尾给出 `POST /2/tweets` 的计数（必须是 0）。

## 一个字都不改数据

本脚本只 `create_agent_run` + `graph.ainvoke`（这两步是"跑一次 Agent"的定义，
它会写自己的 checkpoint 与 decision trace），**不发布、不改 draft、不碰
published_post**。报告写进 `docs/`，按仓库既有惯例（见 `scripts/smoke_7.py`）。

成本：一次运行 = 十几次真实 LLM 调用 + 若干次只读 X 请求。跑之前先看清这一点。

运行：
    python scripts/smoke_context.py
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agent.context import context_signals, context_text, is_empty  # noqa: E402
from app.agent.schemas import EvidencePack  # noqa: E402
from app.db.checkpointer import create_checkpointer  # noqa: E402
from app.main import build_runtime  # noqa: E402
from app.models import AgentRun  # noqa: E402
from app.repositories import create_agent_run  # noqa: E402
from app.services.x_api import RealXApiClient  # noqa: E402

GOAL = "今天关注美股，找一个能讨论的主题"

REPORT_PATH = ROOT / "docs" / "phase-8b-context-smoke.json"

X_REQUESTS: list[dict[str, str]] = []


def install_read_only_guard() -> None:
    """只读守卫：非 GET 在**发出去之前**抛，且每个请求都留痕。"""
    original = RealXApiClient._request

    async def guarded(self, method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        upper = method.upper()
        X_REQUESTS.append({"method": upper, "path": httpx.URL(url).path})
        if upper != "GET":
            raise AssertionError(f"context smoke 只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


def _pack_of(state) -> EvidencePack | None:
    """把 state 里的证据包还原成对象（checkpoint 里可能已经是一份 dict）。"""
    pack = state.get("evidence_pack")
    if pack is None:
        return None
    if isinstance(pack, dict):
        return EvidencePack.model_validate(pack)
    return pack


def context_detail(pack: EvidencePack | None) -> dict:
    context = getattr(pack, "context", None)
    if context is None:
        return {"available": False, "reason": "本次没有形成话题背景"}
    return {
        "available": not is_empty(context),
        "subject": context.subject,
        "definition": context.definition,
        "why_now": context.why_now,
        "core_tension": context.core_tension,
        "origin": getattr(context.origin, "value", context.origin),
        "source_ids": list(context.source_ids),
        # 渲染出来的实际文本：报告里要能看见 Writer / Critic 到底读到了什么
        "rendered": context_text(context),
    }


def signal_detail(pack: EvidencePack | None, candidates: list) -> list[dict]:
    """逐条候选的确定性陌生读者信号（工作单第十四节）。"""
    signals = context_signals(getattr(pack, "context", None), candidates, pack)
    return [
        {
            "candidate_id": signal.candidate_id,
            "context_missing": signal.context_missing,
            "concept_jump": signal.concept_jump,
            "evidence_hijacking": signal.evidence_hijacking,
            "entry_specifics": signal.entry_specifics,
            "hijacked_fact_id": signal.hijacked_fact_id,
        }
        for signal in signals
    ]


def critic_detail(state) -> list[dict]:
    results = list(state.get("critic_results") or [])
    detail: list[dict] = []
    for result in results:
        fit = getattr(result, "context_fit", None)
        detail.append(
            {
                "candidate_id": result.candidate_id,
                "decision": result.decision,
                "score": result.score,
                "flags": list(result.flags),
                "context_fit": (
                    None
                    if fit is None
                    else {
                        "reader_can_follow": fit.reader_can_follow,
                        "context_missing": fit.context_missing,
                        "concept_jump": fit.concept_jump,
                        "evidence_hijacking": fit.evidence_hijacking,
                    }
                ),
            }
        )
    return detail


async def run_once(runtime) -> dict:
    session_factory = runtime.session_factory
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=GOAL)
        run_id = str(run.id)

    print(f">>> 真跑一次：{GOAL}（run_id={run_id}）", flush=True)
    state = await runtime.graph.ainvoke(
        {"run_id": run_id, "goal": GOAL},
        config={"configurable": {"thread_id": run_id}},
    )

    async with session_factory() as session:
        row = (
            await session.execute(select(AgentRun).where(AgentRun.id == run.id))
        ).scalars().first()

    pack = _pack_of(state)
    candidates = list(state.get("candidates") or [])
    return {
        "run_id": run_id,
        "goal": GOAL,
        "error": getattr(row, "error", None),
        "interrupted": "__interrupt__" in state,
        "topic": state.get("selected_topic") or "",
        "steps": state.get("step_count"),
        "materials": len(state.get("research_item_ids") or []),
        "facts": len(getattr(pack, "facts", []) or []),
        "context": context_detail(pack),
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "label": candidate.label,
                "revision": candidate.revision,
                "content": candidate.content,
            }
            for candidate in candidates
        ],
        "reader_signals": signal_detail(pack, candidates),
        "critic": critic_detail(state),
        "actions": [
            getattr(entry, "action", "")
            for entry in (state.get("decision_trace") or [])
            if getattr(entry, "action", "")
        ],
    }


async def run_() -> int:
    from app.config import settings

    url = settings.langgraph_checkpoint_database_url or settings.database_url
    report: dict = {
        "phase": "8B-context",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "一次真机运行的记录（工作单第十九节）。内容是当天真实 X 检索与真实 LLM 的"
            "产物，**不可复现**——它不是基准，是当时的记录。"
        ),
        "goal": GOAL,
    }

    install_read_only_guard()
    async with create_checkpointer(url) as checkpointer:
        runtime = build_runtime(checkpointer)
        try:
            report["run"] = await run_once(runtime)
        except Exception as exc:  # noqa: BLE001 - 一次失败要如实记下来，不是崩掉
            report["run"] = None
            report["error"] = f"{type(exc).__name__}: {exc}"
            print(f"    **这一轮失败了**：{report['error']}", flush=True)

    report["x_requests"] = {
        "total": len(X_REQUESTS),
        "by_method": {
            method: sum(1 for item in X_REQUESTS if item["method"] == method)
            for method in sorted({item["method"] for item in X_REQUESTS})
        },
        "post_tweets": sum(
            1
            for item in X_REQUESTS
            if item["method"] != "GET" or item["path"].endswith("/2/tweets")
        ),
    }

    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\n报告已写入 {REPORT_PATH}", flush=True)
    print(
        f"X 请求 {report['x_requests']['total']} 次，其中 POST /2/tweets "
        f"{report['x_requests']['post_tweets']} 次",
        flush=True,
    )
    return 0


def main() -> int:
    return asyncio.run(run_())


if __name__ == "__main__":
    raise SystemExit(main())
