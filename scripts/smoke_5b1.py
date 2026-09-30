"""Phase 5B.1 真机只读 smoke run（工作单第二十七 / 三十五节）。

跑五次真实运行：**真实 X 只读检索 + 真实 LLM + 共享 PostgreSQL**，
每次记录工作单第三十八节 L 要求的那七项：

```text
steps / discover_trends / research_calls / candidate_topics /
selected_topic / evidence_count / finish_reason
```

## 这个脚本为什么不放进 evals/

`evals/` 的整套设计前提是**离线、逐位可复现**（见 evals/loader.py 的说明）。
真机 smoke 依赖当天的 X 内容与 LLM 采样，两者都不可复现——所以它必须在
`evals/` 之外，不能混进基准。这里只做**观察与记录**，判定规则一条都不加。

## 只读是怎么被保证的

不靠"记得不要发布"：`RealXApiClient._request` 被换成一层守卫，
每个请求先落进 `X_REQUESTS` 再放行，**方法不是 GET 就直接抛**。
于是一旦有任何代码路径试图发帖，整次 smoke 立刻中断而不是悄悄发出去，
而"这五次运行对 X 发了哪些请求"也留下了一份可核对的清单（工作单第三十八节 M）。

运行：`python scripts/smoke_5b1.py`（需 PostgreSQL 在跑、.env 有 X 与 LLM 凭据）
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 必须显式把仓库根放到 sys.path 最前。
#
# conda 环境的 site-packages 里有一个 `_editable_impl_x_agent.pth`，它把
# **master 检出**（C:\xuexi\py\2026_agent\X_Agent）追加进了 sys.path。直接
# `python scripts/smoke_5b1.py` 时 sys.path[0] 是 scripts/，仓库根还排在
# master 后面，于是 `import app` 会静默拿到 master 的 app —— 跑出来的是
# 另一个版本的代码，而且看不出任何异常。插到最前面就绕开了。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agent.schemas import AgentAction  # noqa: E402
from app.db.checkpointer import create_checkpointer  # noqa: E402
from app.diagnostics import describe  # noqa: E402
from app.main import _thread_config, build_runtime  # noqa: E402
from app.models import AgentRun  # noqa: E402
from app.repositories import (  # noqa: E402
    create_agent_run,
    mark_run_completed,
    mark_run_waiting_for_review,
)
from app.services.x_api import RealXApiClient  # noqa: E402

# 工作单第二十七节的五个目标。Smoke 3 原文是"研究一个我已经明确指定的技术话题"，
# 这里把"明确指定"落到实处：目标里写死了题目，Agent 没有任何理由再去做一轮发现。
SMOKES: tuple[dict[str, str], ...] = (
    {
        "id": "smoke-1",
        "goal": "今天帮我找一个最近值得在中文科技 X 圈讨论的 AI Agent 话题。",
        "watch": "discover_trends → select_topic → research → evidence",
    },
    {
        "id": "smoke-2",
        "goal": "找最近值得讨论的 AI 模型发布话题。",
        "watch": "换一个目标，Agent 能不能发现不同的主题",
    },
    {
        "id": "smoke-3",
        "goal": "研究一下 MCP（Model Context Protocol）在企业级 AI Agent 集成里的采用情况。",
        "watch": "题目已经给定：Agent 不该机械地先做一轮 discover_topics",
    },
    {
        "id": "smoke-4",
        "goal": "找一个最近三天内有明显争议的 AI 话题。",
        "watch": "counterpoints / unknowns 是否真的存在",
    },
    {
        "id": "smoke-5",
        "goal": "今天帮我找一个最近值得在中文科技 X 圈讨论的 AI Agent 话题。",
        "watch": "重复 Smoke 1：同一热点是完全一样，还是候选发生合理变化",
    },
)

REPORT_PATH = ROOT / "docs" / "phase-5b1-smoke-runs.json"

# 对 X 发出的每一次请求。方法 + 路径 + （检索请求）实际发出去的 query 与窗口。
X_REQUESTS: list[dict[str, str]] = []


def install_read_only_guard() -> None:
    """把 X 客户端包成只读的：非 GET 一律拒绝，且每个请求都留痕。"""
    original = RealXApiClient._request

    async def guarded(self, method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        upper = method.upper()
        entry: dict[str, str] = {"method": upper, "path": httpx.URL(url).path}
        if upper == "GET" and entry["path"].endswith("/2/tweets/search/recent"):
            # 检索词与窗口都要落进报告，理由不同：
            #
            # - 窗口（start_time / end_time）是**执行事实**的证据。没有它们，
            #   "72 小时窗口有没有退化成无界的 recent search"只能靠读代码相信。
            # - query 是**非法检索词**的证据。`A | B` 这类词一旦真的发出去，X 会回
            #   400；记录下每一次实际发送的 query，才能证明"窗口外的与非法的东西
            #   都没有出门"，而不是证明"我们改过代码了"。
            params = kwargs.get("params") or {}
            for key in ("query", "start_time", "end_time", "max_results"):
                if params.get(key) is not None:
                    entry[key] = str(params[key])
        X_REQUESTS.append(entry)
        if upper != "GET":
            # 抛在发出去**之前**：这是守卫，不是审计。
            raise AssertionError(f"Phase 5B.1 只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


def _field(entry, name: str, default=None):  # type: ignore[no-untyped-def]
    """从轨迹条目上取字段。对象与 dict 都认。"""
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _actions(trace: list) -> list[str]:
    """轨迹里的动作序列。枚举与裸字符串都归一成字符串。"""
    sequence = []
    for entry in trace:
        action = _field(entry, "action")
        sequence.append(action.value if hasattr(action, "value") else str(action))
    return sequence


def _iso(value) -> str | None:  # type: ignore[no-untyped-def]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _candidates(topics: list) -> list[dict]:
    """候选选题的可核对摘录。

    `trend_reasons` 原样带上——工作单要求 trend_score 可解释，
    那意味着报告里必须能看到"这个分数是由哪几个数算出来的"，
    而不是只有一句"Agent 认为它值得写"。
    """
    return [
        {
            "topic_id": topic.topic_id,
            "title": topic.title,
            "trend_score": topic.trend_score,
            "trend_score_raw": topic.trend_score_raw,
            "qualification": topic.qualification.value,
            "lifecycle": topic.lifecycle.value,
            "author_count": topic.author_count,
            "discussion_count": topic.discussion_count,
            "source_post_ids": list(topic.source_post_ids),
            "latest_seen_at": _iso(topic.latest_seen_at),
            "observed_at": _iso(topic.observed_at),
            "trend_reasons": list(topic.trend_reasons),
        }
        for topic in topics
    ]


def _evidence(pack) -> dict:  # type: ignore[no-untyped-def]
    if pack is None:
        return {"facts": 0, "sources": 0, "consensus": 0, "counterpoints": 0, "unknowns": 0}
    return {
        "facts": len(pack.facts),
        "sources": len(pack.source_ids),
        "consensus": len(pack.consensus),
        "counterpoints": len(pack.counterpoints),
        "unknowns": len(pack.unknowns),
    }


def _traceability(pack, state: dict) -> dict:
    """把 angle → fact → 材料 两跳走一遍，看有没有断的。

    与 evals 里的 RULE-006 同一条链，但这里是在**真机数据**上走：
    基准只能证明"喂进去的那份数据没断"，证明不了这次真跑没断。
    """
    if pack is None:
        return {"checked": 0, "broken": []}

    fact_ids = {fact.fact_id for fact in pack.facts}
    item_ids = set(state.get("research_item_ids") or [])
    broken: list[str] = []

    for fact in pack.facts:
        if not fact.source_ids:
            broken.append(f"{fact.fact_id} 没有出处")
        for source in fact.source_ids:
            if source not in item_ids:
                broken.append(f"{fact.fact_id} 指向不存在的材料 {source}")

    for angle in state.get("angles") or []:
        if not angle.evidence_ids:
            broken.append(f"{angle.angle_id} 没有引用任何事实")
        for evidence_id in angle.evidence_ids:
            if evidence_id not in fact_ids:
                broken.append(f"{angle.angle_id} 指向不存在的事实 {evidence_id}")

    checked = len(pack.facts) + sum(len(a.evidence_ids) for a in state.get("angles") or [])
    return {"checked": checked, "broken": broken}


def _select_step(trace: list) -> int | None:
    for index, entry in enumerate(trace, start=1):
        action = _field(entry, "action")
        value = action.value if hasattr(action, "value") else str(action)
        if value == AgentAction.SELECT_TOPIC.value:
            return index
    return None


def _collect(run_id: str, final_state: dict, x_slice: list[dict], db_row: dict) -> dict:
    trace = final_state.get("decision_trace") or []
    sequence = _actions(trace)
    topics = final_state.get("candidate_topics") or []
    discover_steps = [i for i, a in enumerate(sequence, 1) if a == AgentAction.DISCOVER_TOPICS.value]

    finish_reason = None
    for entry in reversed(trace):
        if _field(entry, "action") in (AgentAction.FINISH, AgentAction.FINISH.value):
            finish_reason = _field(entry, "reason")
            break

    research_actions = {AgentAction.RESEARCH_TOPIC.value, AgentAction.DEEPEN_RESEARCH.value}
    select_step = _select_step(trace)

    return {
        "run_id": run_id,
        "steps": len(trace),
        "action_sequence": sequence,
        "discover_trends": {
            "ran": bool(discover_steps),
            "step": discover_steps[0] if discover_steps else None,
            "candidate_count": len(topics),
            "qualified_count": sum(
                1 for t in topics if t.qualification.value == "qualified"
            ),
        },
        "research_calls": {
            "steps": sum(1 for a in sequence if a in research_actions),
            # 真正打到 X 的检索次数。与上面的步数分开记：一次 deepen_research
            # 也可以不发请求（预算或缓存），两者混在一个数里就都对不上了。
            "x_search_requests": sum(
                1 for r in x_slice if r["path"].endswith("/2/tweets/search/recent")
            ),
            "x_requests_total": len(x_slice),
            # 非 GET 请求数。守卫会在发出去之前抛（见 install_read_only_guard），
            # 所以这个数正常恒为 0——留着它是为了让"只读"这件事在报告里可查，
            # 而不是只能靠"脚本里有那个守卫"相信。
            "x_post_requests": sum(1 for r in x_slice if r["method"] != "GET"),
        },
        # 这次运行**真正发给 X** 的每一次检索：检索词 + 窗口（工作单第二十节）。
        # 窗口是执行事实，query 是"非法检索词有没有出门"的证据。
        "x_search_windows": [
            {
                "query": r.get("query"),
                "start_time": r.get("start_time"),
                "end_time": r.get("end_time"),
                "max_results": r.get("max_results"),
            }
            for r in x_slice
            if r["path"].endswith("/2/tweets/search/recent")
        ],
        "candidate_topics": _candidates(topics),
        "selected_topic": final_state.get("selected_topic"),
        "selected_topic_step": select_step,
        # 落在库里的那两个值。它们与上面那个 state 值是**分头取的**：
        # "选出来了"和"记下来了"本来就是两件事，这个缺陷就是两者长期不一致。
        "agent_run_topic": db_row.get("topic"),
        "run_status": db_row.get("status"),
        "critic_count": sequence.count(AgentAction.CRITICIZE.value),
        "revision_count": final_state.get("revision_count"),
        "research_count": final_state.get("research_count"),
        "evidence_count": _evidence(final_state.get("evidence_pack")),
        "evidence_traceability": _traceability(final_state.get("evidence_pack"), final_state),
        "angles": [
            {
                "angle_id": angle.angle_id,
                "claim": angle.claim,
                "evidence_ids": list(angle.evidence_ids),
                "confidence": angle.confidence,
            }
            for angle in final_state.get("angles") or []
        ],
        "finish_reason": finish_reason,
        "finish_action": sequence[-1] if sequence else None,
        "interrupted": "__interrupt__" in final_state,
        "draft_chars": len(final_state.get("draft_content") or ""),
    }


#: 会让 X 回 400 的字符（与 `app/agent/search_query.py::UNUSABLE_CHARS` 同一套）。
#: 这里**独立抄一份**而不是 import：这份清单是验收判据，它必须能独立地回答
#: "实际发出去的检索词里有没有非法字符"，而不是复用被测代码自己的判断——
#: 复用的话，黑名单少一个字符时判据也跟着少一个，两边一起错。
ILLEGAL_QUERY_CHARS = ("/", ":", "(", ")", "|")


def _acceptance(record: dict) -> dict:
    """工作单第二十一节的逐条判定。**只判定，不改任何东西。**

    每一条都落在报告里已有的原始字段上，所以判据说"通过"时，读的人可以
    顺着同一份记录自己复核一遍——这是它和"跑完脚本看退出码"的区别。
    """
    searches = record.get("x_search_windows") or []
    windows = [(w.get("start_time"), w.get("end_time")) for w in searches]
    candidates = record.get("candidate_topics") or []
    queries = [w.get("query") or "" for w in searches]

    checks = {
        # 传输层：真的打到过 X 并且没抛错。这两个条件缺一不可——只判"没报错"
        # 的话，一次根本没发请求的运行也会算通过。
        "x_get_ok": record.get("error") is None
        and (record.get("research_calls", {}).get("x_search_requests") or 0) > 0,
        # 模型层同理。这里只能由"整条没失败"来推定：任何一次 LLM 调用失败都会
        # 抛穿工具、让 run 判 failed（见 run_one 的 except）。
        "llm_ok": record.get("error") is None,
        # Agent 真的把流程走完了：有轨迹、有稿子、停在人工审核
        "agent_completed": bool(record.get("steps")) and bool(record.get("draft_chars")),
        "candidate_count_gt_1": len(candidates) > 1,
        "real_source_post_ids": bool(candidates)
        and all(t.get("source_post_ids") for t in candidates),
        "trend_score_present": bool(candidates)
        and all(isinstance(t.get("trend_score"), (int, float)) for t in candidates),
        "evidence_traceable": bool(record.get("evidence_traceability", {}).get("checked"))
        and not record.get("evidence_traceability", {}).get("broken"),
        "waiting_for_review": record.get("interrupted") is True
        and record.get("run_status") == "waiting_for_review",
        # Defect 1 的判据：定出来的题目必须真的落进 `agent_run.topic`
        "topic_persisted": bool(record.get("selected_topic"))
        and record.get("agent_run_topic") == record.get("selected_topic"),
        # Defect 2 的判据：**每一次**检索都带窗口。少数一次不带就整条判否——
        # 这正是 smoke-3 当初"5 条材料里 2 条在窗口外"的成因。
        "all_searches_windowed": bool(searches)
        and all(start and end for start, end in windows),
        # Defect 3 的判据：非法字符一个都没出门
        "no_illegal_query": bool(queries)
        and not any(
            char in query for query in queries for char in ILLEGAL_QUERY_CHARS
        ),
        # 只读：这次运行对 X 的非 GET 请求必须是 0
        "no_real_post": record.get("research_calls", {}).get("x_post_requests") == 0,
    }
    return {"checks": checks, "passed": all(checks.values())}


async def run_one(runtime, smoke: dict[str, str]) -> dict:  # type: ignore[no-untyped-def]
    """跑一次真机运行，返回它的观察记录。"""
    session_factory = runtime.session_factory
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=smoke["goal"])
    run_id = str(run.id)

    x_start = len(X_REQUESTS)
    started = datetime.now(timezone.utc)
    failure: str | None = None
    final_state: dict = {}

    try:
        final_state = await runtime.graph.ainvoke(
            {"run_id": run_id, "goal": smoke["goal"]}, config=_thread_config(run_id)
        )
    except Exception as exc:  # noqa: BLE001
        # 一次失败不该让后面四次也跑不了：记下来，继续。
        #
        # 用 `describe()` 而不是自己拼 `f"{type}: {exc}"`：报告里的 `error` 字段
        # 是这份记录**唯一的**线索，而 httpx 连接失败抛的 `ConnectError("")`
        # 的 str() 是空的（原因在 __cause__），自己拼就会留下
        # `XApiTransportError: X API request could not be sent:` 这种读到尽头
        # 也没有信息的记录——smoke-2 / smoke-4 当时就是这样，白跑一遍还不知道
        # 为什么。与发布路径共用同一个渲染（见 app/diagnostics.py）。
        failure = describe(exc)

    # 状态与 run 行保持一致：Graph 停在 human_review 就写 waiting_for_review，
    # 和 app/main.py::create_run 一字不差。**注意这里没有发布这一步**。
    async with session_factory() as session:
        if failure:
            from app.repositories import mark_run_failed

            await mark_run_failed(session, run_id, failure)
        elif "__interrupt__" in final_state:
            await mark_run_waiting_for_review(session, run_id)
        else:
            await mark_run_completed(session, run_id)

    # 题目与状态**从库里读回来**，不是从 state 拿。这是刻意的：
    # Defect 1 恰好就是"state 里有、库里没有"，从 state 取等于把那个缺陷
    # 从报告里洗掉。读库这一步让报告能直接回答"到底存进去了没有"。
    async with session_factory() as session:
        row = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalars().one()
        db_row = {"topic": row.topic, "status": row.status}

    record = {
        "id": smoke["id"],
        "goal": smoke["goal"],
        "watch": smoke["watch"],
        "started_at": started.isoformat(),
        "elapsed_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "error": failure,
    }
    record.update(_collect(run_id, final_state, X_REQUESTS[x_start:], db_row))
    record["acceptance"] = _acceptance(record)
    return record


def _print_summary(records: list[dict]) -> None:
    print()
    print("=" * 78)
    for record in records:
        print(f"{record['id']}  run_id={record['run_id']}  {record['elapsed_seconds']}s")
        print(f"  goal        {record['goal']}")
        if record["error"]:
            print(f"  ERROR       {record['error']}")
            continue
        discover = record["discover_trends"]
        print(
            f"  steps       {record['steps']}  "
            f"discover={'step ' + str(discover['step']) if discover['ran'] else '没跑'}"
            f"（候选 {discover['candidate_count']}，qualified {discover['qualified_count']}）"
        )
        print(
            f"  research    {record['research_calls']['steps']} 步 / "
            f"{record['research_calls']['x_search_requests']} 次 X 检索"
        )
        print(f"  selected    {record['selected_topic']}")
        evidence = record["evidence_count"]
        print(
            f"  evidence    {evidence['facts']} 条事实 / {evidence['sources']} 个来源 / "
            f"共识 {evidence['consensus']} / 异议 {evidence['counterpoints']} / "
            f"未知 {evidence['unknowns']}"
        )
        print(f"  finish      {record['finish_action']}  {record['finish_reason']}")
        print("  trace       " + " → ".join(record["action_sequence"]))
        print(
            f"  topic       state={record['selected_topic']!r}  "
            f"db={record['agent_run_topic']!r}  status={record['run_status']}"
        )
        for search in record["x_search_windows"]:
            print(
                f"  x search    [{search['start_time']} .. {search['end_time']}]  "
                f"q={search['query']!r}"
            )
        for topic in record["candidate_topics"]:
            print(
                f"    [{topic['topic_id']}] {topic['trend_score']:>6.2f} "
                f"{topic['qualification']:<11} {topic['title'][:52]}"
            )
        if record["evidence_traceability"]["broken"]:
            print(f"  !! 追溯断链 {record['evidence_traceability']['broken']}")
        failed = [name for name, ok in record["acceptance"]["checks"].items() if not ok]
        verdict = "通过" if record["acceptance"]["passed"] else "未通过 " + ", ".join(failed)
        print(f"  验收        {verdict}")
    print("=" * 78)
    methods: dict[str, int] = {}
    for request in X_REQUESTS:
        methods[request["method"]] = methods.get(request["method"], 0) + 1
    print(f"对 X 的请求共 {len(X_REQUESTS)} 次，按方法：{methods or '{}'}")
    print(f"（其中 POST = {methods.get('POST', 0)}）")
    passed = sum(1 for r in records if r.get("acceptance", {}).get("passed"))
    print(f"验收结果：{passed}/{len(records)} 次全部满足工作单第二十一节")


async def main(argv: list[str]) -> int:
    install_read_only_guard()

    from app.config import settings

    # 允许只跑其中几次（`python scripts/smoke_5b1.py smoke-1`）：调试窗口/检索词
    # 这类改动时不必每次都烧掉五次真实调用的额度。只跑子集时**不覆盖**报告文件，
    # 免得一份只有一两条的记录冒充"五次 smoke 的完整记录"。
    selected = [s for s in SMOKES if not argv or s["id"] in argv]
    if argv and not selected:
        print(f"没有匹配的 smoke：{argv}（可选 {[s['id'] for s in SMOKES]}）")
        return 2
    full_run = len(selected) == len(SMOKES)

    url = settings.langgraph_checkpoint_database_url or settings.database_url
    records: list[dict] = []

    async with create_checkpointer(url) as checkpointer:
        runtime = build_runtime(checkpointer)
        for smoke in selected:
            print(f"\n>>> {smoke['id']}: {smoke['goal']}", flush=True)
            record = await run_one(runtime, smoke)
            records.append(record)
            print(f"    完成：{record['steps']} 步，{record['elapsed_seconds']}s", flush=True)

    _print_summary(records)

    if not full_run:
        print("\n只跑了部分 smoke，不写报告文件。")
        return 0 if all(r["error"] is None for r in records) else 1

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "phase": "5B.1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "note": (
                    "5 次真机只读 smoke run 的观察记录（工作单第二十七 / 三十八节 L）。"
                    "内容是当天真实 X 检索与真实 LLM 的产物，因此**不可复现**——"
                    "它不是基准，是当时的记录。"
                ),
                "x_requests": {"total": len(X_REQUESTS), "by_method": _method_counts()},
                "network": _network_note(),
                # 工作单第二十一节的整批判定。它与下面的 `runs` 是同一份数据的
                # 两种读法：判定只引用已记录的字段，读的人可以自己复核。
                "acceptance": {
                    "criteria": "工作单第二十一节",
                    "passed": sum(
                        1 for r in records if r.get("acceptance", {}).get("passed")
                    ),
                    "total": len(records),
                    "all_passed": all(
                        r.get("acceptance", {}).get("passed") for r in records
                    ),
                },
                "runs": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\n记录已写入 {REPORT_PATH}")
    return 0 if all(r["error"] is None for r in records) else 1


def _method_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for request in X_REQUESTS:
        counts[request["method"]] = counts.get(request["method"], 0) + 1
    return counts


def _network_note() -> dict:
    """记下这批运行是**怎么到达 X 的**。

    2026-09-29 这天，本机直连 `api.x.com` 时通时不通（TLS 握手被中断），最后是
    走本机代理跑完的。请求本身两条路一样，但对读记录的人来说这不该是隐形的：
    "哪一次是直连、哪一次走了代理"决定了下次同样跑出问题时该先怀疑什么。
    httpx 只认环境变量（不读 Windows 注册表的系统代理），所以这里看的就是它们。
    """
    keys = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    proxy = next((os.environ[key] for key in keys if os.environ.get(key)), None)
    return {"via_proxy": proxy, "direct": proxy is None}


if __name__ == "__main__":
    # 与 run.py 同一个理由：psycopg3 的异步驱动不能用 Windows 默认的
    # ProactorEventLoop，否则第一次建连就失败（见 app/loops.py）。
    from app.loops import selector_loop_factory

    raise SystemExit(asyncio.run(main(sys.argv[1:]), loop_factory=selector_loop_factory))
