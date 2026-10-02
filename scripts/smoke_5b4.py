"""Phase 5B.4 的 smoke（工作单第四十一节）。

工作单列了五个。**本阶段只跑得动两个**，另外三个跑不了——那就如实记成
"跑不了"并写明原因，不拿别的东西顶替（工作单第三十五节）。

| Smoke | 看什么 | 本阶段 |
|---|---|---|
| 1 | 历史帖子 `PublishedPost → X metrics snapshot` 的数据链 | **跑不了** |
| 2 | 多快照（Day 0 / 1 / 3）的时间序列 | **跑不了** |
| 3 | 小样本 `n < threshold` → `insufficient_sample` | 跑（合成观察） |
| 4 | 样本足够 → 生成 `FeedbackReport` | **跑不了** |
| 5 | Agent 读 Feedback Context；没有也照常跑 | 跑（合成上下文） |

## 为什么 1 / 2 / 4 跑不了

它们问的都是**我们自己发出去的东西后来怎么样**，而那需要库里真的有已发布帖子：

```text
post_snapshots=0
published_post=0 行
```

一条都没有。这不是"没跑到"，是**没有可跑的对象**。工作单第三十五节要求
数据不足时不许编——所以这三条记 `blocked`，不记 `passed`，也不拿合成的
帖子冒充实测。

Smoke 2 / 4 想验证的**性质**（时间序列分档、样本足够时产出报告）在
`evals/feedback/selftest.py` 里有确定性的离线对照（`multiple_windows` /
`healthy_sample` 两个 case）。那份对照证明的是**算法**，不是**数据链**；
两者的区别正是 1 / 2 / 4 仍然算"没跑"的原因。

## Smoke 5 用的是合成上下文，且**不写数据库**

真机上今天不可能有反馈（没有已发布帖子），所以拿一份合成的 `FeedbackContext`
直接注入初始 state。刻意**不往库里塞假的 `published_post`**：那张表是
"我们到底发出去了什么"的审计记录，往里面写一条没发过的帖子，就是把
审计记录变成一份假的。

## 绝不发布

与 `scripts/smoke_5b1/5b2/5b3.py` 同一套只读守卫：`RealXApiClient._request`
被替换，非 GET **在发出去之前**就抛。报告末尾给出请求总数、按方法的分组，
以及 `POST /2/tweets` 的计数（工作单第三十三节：必须是 0）。

运行：`python scripts/smoke_5b4.py`（需 PostgreSQL 在跑、.env 有 X 与 LLM 凭据）
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 与 smoke_5b1 / 5b2 / 5b3 同一个理由：仓库根必须排在 sys.path 最前，否则
# `import app` 可能静默拿到 editable 安装指向的另一个检出。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agent.prompts import format_feedback_context  # noqa: E402
from app.db.checkpointer import create_checkpointer  # noqa: E402
from app.feedback.report import build_feedback_context, build_report  # noqa: E402
from app.feedback.schema import FeedbackSignalKind, SnapshotObservation  # noqa: E402
from app.feedback.signals import (  # noqa: E402
    MIN_SAMPLES_FOR_COMPARISON,
    find_causal_language,
)
from app.main import build_runtime  # noqa: E402
from app.models import AgentRun  # noqa: E402
from app.repositories import create_agent_run  # noqa: E402
from app.services.x_api import RealXApiClient  # noqa: E402

REPORT_PATH = ROOT / "docs" / "phase-5b4-smoke-runs.json"

#: 同一次 smoke 里的两轮都问同一件事，换目标就是换问题。
GOAL = "找一个值得讨论的 AI Agent 话题。"

#: 合成观察的起点。写死而不是 `now()`：这份记录要能被人拿去复算，
#: 一个跟着运行时刻漂移的时间戳做不到这件事。
PUBLISHED_AT = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)

X_REQUESTS: list[dict[str, str]] = []

#: 会做比较的信号种类。低于样本下限时**一个都不许出现**。
COMPARATIVE_KINDS = frozenset(
    {
        FeedbackSignalKind.ATTENTION,
        FeedbackSignalKind.CONVERSATION,
        FeedbackSignalKind.AMPLIFICATION,
        FeedbackSignalKind.SHARE,
    }
)


def install_read_only_guard() -> None:
    """只读守卫：非 GET 在**发出去之前**抛，且每个请求都留痕。"""
    original = RealXApiClient._request

    async def guarded(self, method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        upper = method.upper()
        entry: dict[str, str] = {"method": upper, "path": httpx.URL(url).path}
        if upper == "GET":
            params = kwargs.get("params") or {}
            for key in ("query", "start_time", "end_time", "max_results"):
                if params.get(key) is not None:
                    entry[key] = str(params[key])
        X_REQUESTS.append(entry)
        if upper != "GET":
            raise AssertionError(f"Phase 5B.4 smoke 只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


def _observations(*, samples: int, window_hours: int = 24) -> list[SnapshotObservation]:
    """合成的一条时间线：每次观察晚一小时，计数单调上涨。

    计数一律是**读到了的**（非 None）。`missing_metric` 那条路另有单测，
    这里不混进来——一个 smoke 里混两个变量，出问题时说不清是哪个。
    """
    return [
        SnapshotObservation(
            snapshot_id=f"snap-{i:02d}",
            x_post_id="1900000000000000001",
            published_post_id="post-1",
            observed_at=PUBLISHED_AT + timedelta(hours=i),
            hours_since_publish=float(i),
            likes=10 + 3 * i,
            replies=1 + i,
            reposts=1,
            quotes=i % 2,
        )
        for i in range(1, samples + 1)
    ]


# ---------------------------------------------------------------------------
# Smoke 3：小样本必须走 insufficient_sample
# ---------------------------------------------------------------------------


def smoke_3_small_sample() -> dict:
    """n 低于下限 → **只说 n**，一个比较型信号都不产出。

    这是工作单第十七节最直接的检验：不许把"样本不够"伪装成"已经得出了结论"。
    """
    n = MIN_SAMPLES_FOR_COMPARISON - 1
    report = build_report(_observations(samples=n))
    kinds = [signal.kind for signal in report.signals]

    assert report.sample_count == n, report.sample_count
    assert kinds.count(FeedbackSignalKind.INSUFFICIENT_SAMPLE) == 1, kinds
    assert not (set(kinds) & COMPARATIVE_KINDS), f"低于下限却给出了比较：{kinds}"

    # 唯一那条信号必须带着 n，且**不能**出现比较口径的字段
    guard = next(s for s in report.signals if s.kind is FeedbackSignalKind.INSUFFICIENT_SAMPLE)
    assert guard.sample_count == n, guard.sample_count
    assert guard.comparison_group is None, guard.comparison_group
    assert guard.confidence is None, guard.confidence
    assert guard.evidence, "insufficient_sample 也必须指得出它依据了哪些观察"

    causal = {s.statement: find_causal_language(s.statement) for s in report.signals}
    causal = {k: v for k, v in causal.items() if v}
    assert not causal, f"信号里出现了因果措辞：{causal}"

    context = build_feedback_context(report)
    rendered = format_feedback_context(context)

    # Writer 拿到的是**"样本不够，什么都不说"**这一句，而不是一段分布。
    # 这一点必须肉眼可查：把它换成一组数字，就是把"样本不足"渲染成了一条结论。
    assert guard.statement in rendered, rendered
    assert f"样本：{n} 条观察" in rendered, rendered

    return {
        "available": True,
        "threshold": MIN_SAMPLES_FOR_COMPARISON,
        "sample_count": n,
        "signal_kinds": [k.value for k in kinds],
        "insufficient_sample_count": kinds.count(FeedbackSignalKind.INSUFFICIENT_SAMPLE),
        "comparative_signal_count": len(set(kinds) & COMPARATIVE_KINDS),
        "guard_statement": guard.statement,
        "guard_evidence_count": len(guard.evidence),
        "causal_hits": {},
        "writer_block": rendered,
        "writer_block_chars": len(rendered),
        "note": (
            "低于下限时上下文里没有任何可被当成结论的分布：Writer 拿到的唯一一句"
            "反馈就是「观察只有 4 条，低于下限，不产出比较」。"
        ),
    }


# ---------------------------------------------------------------------------
# Smoke 5：Agent 读反馈 / 没有反馈也照常跑
# ---------------------------------------------------------------------------


async def run_agent(runtime, session_factory, goal: str, *, context=None) -> dict:
    """真跑一次（真实 X 检索 + 真实 LLM）。`context` 非空时注入初始 state。"""
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=goal)
        run_id = str(run.id)

    initial: dict = {"run_id": run_id, "goal": goal}
    if context is not None:
        initial["feedback_context"] = context

    state = await runtime.graph.ainvoke(
        initial, config={"configurable": {"thread_id": run_id}}
    )
    async with session_factory() as session:
        row = (await session.execute(select(AgentRun).where(AgentRun.id == run.id))).scalars().first()

    return {
        "run_id": run_id,
        "goal": goal,
        "state": state,
        "interrupted": "__interrupt__" in state,
        "db_status": getattr(row, "status", None),
        "steps": state.get("step_count"),
        "topic": state.get("selected_topic") or "",
    }


def _feedback_block_of(run: dict) -> str:
    """这一轮 Writer 实际会拿到的那段文本。

    生产渲染器（`format_feedback_context`）是纯函数，作用在这次运行结束时
    state 里的那个 context 上。**这是代理，不是抓包**：真正"prompt 里确实有
    这句话"的抓包级证据在 `tests/test_feedback_writer_integration.py`
    （那边用替身 LLM 拿到了真实的 prompt 字符串）。这里证明的是真机上
    state 里的 context 确实非空、并且渲染得过观察性守卫。
    """
    return format_feedback_context(run["state"].get("feedback_context"))


def smoke_5_with_feedback(run: dict) -> dict:
    """有反馈：Writer 能看到**结构化的观察**，而不是一段自由文本或一条指令。"""
    context = run["state"].get("feedback_context")
    assert context is not None, "注入的 feedback_context 没有留在 state 里"
    assert not context.is_empty, "注入的上下文被判成空"

    signals = list(context.signals)
    assert signals, "有快照却一条信号都没有"
    kinds = [s.kind for s in signals]

    block = _feedback_block_of(run)
    # 守卫把它丢掉时会换成另一句话——那种情况下这一段就**没有**到达 Writer
    assert "未通过观察性检查" not in block, block
    assert "不是本次的写作要求" in block, block
    assert f"{context.snapshot_count} 条观察" in block, block
    assert all(s.statement in block for s in signals), "有信号没进 block"

    causal = {k: v for k, v in ((s.statement, find_causal_language(s.statement)) for s in signals) if v}
    assert not causal, f"信号里出现了因果措辞：{causal}"

    # 工作单第四十二节：不许把"这类帖子以前有 300 likes"变成指令，
    # 也不许把过去帖子的正文搬进来。
    for banned in ("写作要求：", "必须", "应该采用", "300"):
        assert banned not in block, f"Writer 的反馈块里出现了 {banned!r}"

    return {
        "available": True,
        "run_id": run["run_id"],
        "interrupted": run["interrupted"],
        "steps": run["steps"],
        "snapshot_count": context.snapshot_count,
        "post_count": context.post_count,
        "sample_count": context.sample_count,
        "signal_kinds": [k.value for k in kinds],
        "signal_count": len(signals),
        "comparison_group_is_null": all(s.comparison_group is None for s in signals),
        "confidence_is_null": all(s.confidence is None for s in signals),
        "evidence_non_empty": all(bool(s.evidence) for s in signals),
        "writer_block_chars": len(block),
        "writer_block": block,
        "note": (
            "合成上下文注入初始 state；没有往 published_post 写任何一行。"
            "prompt 抓包级证据见 tests/test_feedback_writer_integration.py。"
        ),
    }


def smoke_5_without_feedback(run: dict) -> dict:
    """没有反馈：整条链路照常跑完，`{{FEEDBACK}}` 是一段说明而不是空占位符。"""
    context = run["state"].get("feedback_context")
    assert context is not None, "没有反馈时也必须有 context（中性空的那个）"

    block = _feedback_block_of(run)
    assert "{{" not in block, block
    neutral = context.is_empty
    assert neutral, (
        "库里居然已经有快照了，这一轮读到的不是空上下文——"
        "'没有反馈也能跑'这条在真机上没被检验到"
    )

    # 这一轮照样走到人工审核，且留下了正文：反馈缺席不影响生产的可用性。
    assert run["interrupted"], "没有反馈时运行没有停在人工审核"

    return {
        "available": True,
        "run_id": run["run_id"],
        "interrupted": run["interrupted"],
        "steps": run["steps"],
        "context_is_empty": True,
        "writer_block": block,
        "note": "空上下文渲染成一段说明（不是空白、也不是未替换的占位符）。",
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _method_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in X_REQUESTS:
        counts[entry["method"]] = counts.get(entry["method"], 0) + 1
    return counts


def _blocked(reason: str) -> dict:
    return {"available": False, "blocked": True, "reason": reason}


NO_PUBLISHED_POSTS = (
    "库里 published_post 一行都没有（也没有任何 post_metrics_snapshot），"
    "所以这条数据链没有可跑的对象。它不是'没跑到'，是**没有东西可跑**。"
    "工作单第三十五节：数据不足时不编，如实记为 blocked。"
)
NO_LIVE_X = (
    "本阶段锁定决定：不做真实 X 调用（工作单第三十三节只说 GET 允许，"
    "而工作单第四十一节这几条 smoke 要求的是历史真实帖子的回读）。"
)


async def run_() -> int:
    from app.config import settings

    url = settings.langgraph_checkpoint_database_url or settings.database_url
    report: dict = {
        "phase": "5B.4",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "工作单第四十一节五条 smoke 的记录。Smoke 3 / 5 在合成数据上跑；"
            "Smoke 1 / 2 / 4 因为没有已发布帖子而**跑不了**，记成 blocked。"
            "内容含当天真实 X 检索与真实 LLM 的产物，因此**不可复现**——"
            "它不是基准，是当时的记录。"
        ),
        "goal": GOAL,
        "smokes": {},
    }

    # --- Smoke 3（离线，不需要运行时） ---
    print(">>> Smoke 3：小样本 → insufficient_sample", flush=True)
    try:
        report["smokes"]["3_small_sample"] = smoke_3_small_sample()
        print("    通过。", flush=True)
    except Exception as exc:  # noqa: BLE001 - 报告优先，异常要有去处
        report["smokes"]["3_small_sample"] = {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
        print(f"    **失败了**：{exc}", flush=True)

    # --- Smoke 5（真跑两轮） ---
    context = build_feedback_context(build_report(_observations(samples=8)))
    failures: dict[str, str] = {}
    runs: dict[str, dict] = {}

    async with create_checkpointer(url) as checkpointer:
        runtime = build_runtime(checkpointer)
        session_factory = runtime.session_factory

        print(">>> Smoke 5a：有反馈（合成上下文）", flush=True)
        try:
            runs["with"] = await run_agent(runtime, session_factory, GOAL, context=context)
            report["smokes"]["5a_agent_reads_feedback"] = smoke_5_with_feedback(runs["with"])
            print("    通过。", flush=True)
        except Exception as exc:  # noqa: BLE001
            failures["with"] = f"{type(exc).__name__}: {exc}"
            report["smokes"]["5a_agent_reads_feedback"] = {
                "available": False,
                "reason": failures["with"],
            }
            print(f"    **失败了**：{exc}", flush=True)

        print(">>> Smoke 5b：没有反馈", flush=True)
        try:
            runs["without"] = await run_agent(runtime, session_factory, GOAL)
            report["smokes"]["5b_agent_without_feedback"] = smoke_5_without_feedback(
                runs["without"]
            )
            print("    通过。", flush=True)
        except Exception as exc:  # noqa: BLE001
            failures["without"] = f"{type(exc).__name__}: {exc}"
            report["smokes"]["5b_agent_without_feedback"] = {
                "available": False,
                "reason": failures["without"],
            }
            print(f"    **失败了**：{exc}", flush=True)

    # --- 跑不了的三条 ---
    report["smokes"]["1_historical_post_chain"] = _blocked(NO_PUBLISHED_POSTS + " " + NO_LIVE_X)
    report["smokes"]["2_snapshot_time_series"] = _blocked(
        NO_PUBLISHED_POSTS
        + " 时间序列的**分档口径**另在 evals/feedback/selftest.py::multiple_windows "
        "有离线对照，但那证明的是算法，不是数据链。"
    )
    report["smokes"]["4_feedback_report_enough_samples"] = _blocked(
        NO_PUBLISHED_POSTS
        + " 样本足够时产出报告的**算法**另在 evals/feedback/selftest.py::healthy_sample "
        "有离线对照，但那证明的是算法，不是真实数据。"
    )

    report["runs"] = [
        {k: v for k, v in run.items() if k != "state"} for run in runs.values()
    ]
    report["failed_runs"] = failures
    report["x_requests"] = {
        "total": len(X_REQUESTS),
        "by_method": _method_counts(),
        "post_tweets": sum(
            1 for r in X_REQUESTS if r["method"] != "GET" and r["path"].endswith("/2/tweets")
        ),
        "queries": sorted({r["query"] for r in X_REQUESTS if "query" in r}),
    }

    print("\n--- 请求账（工作单第三十三节） ---")
    print(f"总请求 {len(X_REQUESTS)}，按方法 {_method_counts()}")
    print(f"POST /2/tweets：{report['x_requests']['post_tweets']}（必须是 0）")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    document: dict = {}
    if REPORT_PATH.exists():
        try:
            loaded = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
        except json.JSONDecodeError:
            document = {}
    document.setdefault("phase", "5B.4")
    document.setdefault("note", "每次 smoke 一份记录，累积保存在 runs 里。")
    document.setdefault("runs", []).append(report)
    REPORT_PATH.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\n记录已写入 {REPORT_PATH}")

    if failures:
        print(f"注意：{len(failures)} 轮运行失败 —— {json.dumps(failures, ensure_ascii=False)}")
        return 1
    return 0


def main() -> int:
    install_read_only_guard()
    return asyncio.run(run_())


if __name__ == "__main__":
    raise SystemExit(main())
