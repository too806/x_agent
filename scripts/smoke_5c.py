"""Phase 5C 的 smoke（工作单第三十八节）。

工作单列了五条。**五条在这里都跑**，但它们跑的地方不一样，报告里逐条写明：

| Smoke | 看什么 | 数据 | 断言什么 |
|---|---|---|---|
| 1 | 真实已发布帖子 → snapshots → performance observation | 真机库（只读） | 数据链真的连得上 |
| 2 | 真实当前数据 → gate false | 真机库（只读） | `learning_ready=false` + `insufficient_sample` |
| 3 | 离线 5+ 帖 → 合法 cohort | 合成 | `learning_ready=true` 且 `automatic_modification=false` |
| 4 | 缺 metrics → gate false | 合成 | `missing_metrics`，缺失**不补 0** |
| 5 | 不同 observation window → gate false | 合成 | `inconsistent_window`，不比两个窗口 |

前两条读真机库，后三条在合成记录上跑。**这不是偷工**：Smoke 1 / 2 证明的是
**数据链**（库里的帖子确实能变成一条 performance observation），Smoke 3 / 4 / 5
证明的是**闸门**（四种拒绝必须各自发生）。真机上只有 1 条帖子、2 条快照，
它既不可能凑出 5 条帖子，也不可能同时展示"窗口不一致"——拿合成数据冒充真机数据
才是偷工。工作单第三十一、三十二节：数据不足时不编，如实说这份报告哪一段
基于什么。

## 绝不发布，而且**这一次真的发 GET**

`RealXApiClient._request` 被换成前几阶段同一套只读守卫：**非 GET 在发出去之前就抛**。
工作单第三十节允许 GET、禁止 `POST /2/tweets`；报告末尾给出按方法分组的请求计数，
`POST /2/tweets` 必须是 0。

与 5B.4 的 smoke 不同，这里**尝试**一次真实的只读 GET（回读那条真实帖子的公开计数）。
目的是证明"只读这条路是通的"，而不是证明"数据够"。拿不到就如实记 `blocked`
（断网、无凭据、本地代理没开都算），**不让整份 smoke 失败**——一次外部依赖不可用
不该让四条本地断言的结果一起丢掉。

## 一个字都不写

本脚本不写数据库、不写 checkpoint、不修改任何 metrics。它读到的每一样东西
都原样出现在报告里，供人核对"当时读到的是这个"。

运行：`python scripts/smoke_5c.py`（需 PostgreSQL 在跑、.env 有配置；
X 凭据可选——没有就只记 `blocked`）
"""

import asyncio
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 与其它脚本同一个理由：仓库根必须排在 sys.path 最前，否则 `import app`
# 可能静默拿到 editable 安装指向的另一个检出。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import repositories  # noqa: E402
from app.db.checkpointer import create_checkpointer  # noqa: E402
from app.feedback.metrics import window_bucket  # noqa: E402
from app.feedback.profile import build_post_performance_profile  # noqa: E402
from app.feedback.schema import SnapshotObservation  # noqa: E402
from app.feedback.signals import MIN_SAMPLES_FOR_COMPARISON  # noqa: E402
from app.learning.performance import (  # noqa: E402
    select_window_observation,
    vector_at_window,
)
from app.learning.report import (  # noqa: E402
    build_learning_report,
    build_learning_report_offline,
)
from app.learning.runtime import load_records, record_from_profile  # noqa: E402
from app.learning.schema import (  # noqa: E402
    LearningEvidenceKind,
    PostPerformanceRecord,
)
from app.main import build_runtime  # noqa: E402
from app.services.x_api import RealXApiClient  # noqa: E402
from app.services.x_service import XService  # noqa: E402

REPORT_PATH = ROOT / "docs" / "phase-5c-smoke-runs.json"

#: 合成记录的起点。写死而不是 `now()`：这份记录要能被拿去复算，
#: 一个跟着运行时刻漂移的时间戳做不到这件事。
PUBLISHED_AT = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)

#: Smoke 3 / 4 用的窗口；Smoke 5 里再引入一个**不同**的窗口。
WINDOW = "24-72h"
OTHER_WINDOW = "1-6h"

#: 窗口内的观察时刻。必须落在窗口里：`24-72h` 是 `[24, 72)`，`1-6h` 是 `[1, 6)`。
HOURS_IN_WINDOW = {"24-72h": 48.0, "1-6h": 2.0}

#: 对 X 发出的每一次请求（方法与路径）。报告末尾据此证明"没有写请求"。
X_REQUESTS: list[dict[str, str]] = []

#: 闸门四条判据的名字与顺序（工作单第二十四节）。顺序即优先级。
CHECK_NAMES = ("sample_count", "window_consistent", "metrics_present", "comparison_valid")


def install_read_only_guard() -> None:
    """只读守卫：非 GET 在**发出去之前**抛，且每个请求都留痕。"""
    original = RealXApiClient._request

    async def guarded(self, method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        upper = method.upper()
        entry: dict[str, str] = {"method": upper, "path": httpx.URL(url).path}
        if upper == "GET":
            params = kwargs.get("params") or {}
            if params.get("ids"):
                entry["ids"] = str(params["ids"])
        X_REQUESTS.append(entry)
        if upper != "GET":
            raise AssertionError(f"Phase 5C smoke 只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 合成记录（Smoke 3 / 4 / 5）
# ---------------------------------------------------------------------------


def _synthetic_x_id(index: int) -> str:
    """合成帖子的 x_post_id。

    刻意与真实 id（`2105998…`）同一形状但不同段：报告里一眼能看出哪条是合成的，
    而且它**永远不会**被发出去——Smoke 3/4/5 一次网络请求都不做。
    """
    return f"9000000000000000{index:03d}"


def record(
    index: int,
    *,
    hook: str,
    window: str = WINDOW,
    likes: int | None = None,
    replies: int | None = None,
    quotes: int | None = None,
    reposts: int | None = None,
) -> PostPerformanceRecord:
    """一条合成记录：**一条帖子、一个窗口、一条观察**。

    四个计数各自可空，而且**没有一处 `or 0`**——Smoke 4 整个就建立在
    "传进去的 `None` 出来还是 `None`"这件事上。
    """
    hours = HOURS_IN_WINDOW[window]
    observation = SnapshotObservation(
        snapshot_id=f"snap-{index:02d}",
        x_post_id=_synthetic_x_id(index),
        published_post_id=f"post-{index:02d}",
        observed_at=PUBLISHED_AT + timedelta(hours=hours),
        hours_since_publish=hours,
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
    )
    observations = (observation,)
    return PostPerformanceRecord(
        published_post_id=f"post-{index:02d}",
        x_post_id=_synthetic_x_id(index),
        published_at=PUBLISHED_AT,
        hook_type=hook,
        observation_window=window,
        performance=vector_at_window(observations, window),
        observations=observations,
    )


def _readiness_summary(readiness) -> dict:
    return {
        "learning_ready": readiness.learning_ready,
        "usable_for_future_learning": readiness.usable_for_future_learning,
        "reason": readiness.reason,
        "observation_window": readiness.observation_window,
        "sample_count": readiness.sample_count,
        "post_count": readiness.post_count,
        "automatic_modification": readiness.automatic_modification,
        "checks": [
            {"name": c.name, "passed": c.passed, "detail": c.detail} for c in readiness.checks
        ],
        "limitations": list(readiness.limitations),
    }


def _no_apply_learning() -> dict:
    """工作单第三十六节：**没有** `apply_learning()`，只有评估。

    扫的是包的属性而不是注释：将来有人加回来，这一条会先失败。
    """
    import app.learning as package

    modules = ["", "schema", "performance", "cohort", "evidence", "report", "runtime"]
    found: list[str] = []
    for name in modules:
        module = package if not name else importlib.import_module(f"app.learning.{name}")
        for attr in dir(module):
            if "apply_learning" in attr.lower():
                found.append(f"app.learning.{name or '__init__'}.{attr}")
    assert not found, f"学习层出现了 apply_learning：{found}"
    return {"apply_learning_absent": True, "scanned_modules": len(modules)}


# ---------------------------------------------------------------------------
# Smoke 3：离线 5+ 帖 → 合法 cohort → gate true
# ---------------------------------------------------------------------------


def smoke_3_offline_cohorts() -> dict:
    """两组各 5 条帖子、同一窗口、同一维度 → 闸门放行。

    这是本阶段唯一一条**应当**为 `True` 的路径，所以它必须同时证明另一半：
    `automatic_modification` 仍然是 `False`。"可以拿去学习"与"可以据此改自己"
    是两件事，工作单第二十五节要求后者永远是 `False`。
    """
    records = [
        record(i, hook="question", likes=10 + i) for i in range(1, 6)
    ] + [record(i, hook="statement", likes=30 + i) for i in range(6, 11)]

    report = build_learning_report_offline(records)

    assert report.observation_window == WINDOW, report.observation_window
    assert report.readiness.learning_ready is True, report.readiness
    assert report.readiness.reason == "ready", report.readiness.reason
    assert report.readiness.automatic_modification is False
    assert report.readiness.sample_count == 10, report.readiness.sample_count
    assert tuple(c.name for c in report.readiness.checks) == CHECK_NAMES
    assert all(c.passed for c in report.readiness.checks), report.readiness.checks

    valid = report.valid_comparisons
    assert valid, "两组各 5 条却没有一组比较成立"
    for comparison in valid:
        assert comparison.valid and comparison.invalid_reason is None
        assert comparison.dimension.value == "hook_type", comparison.dimension
        assert comparison.group_a.sample_count >= MIN_SAMPLES_FOR_COMPARISON
        assert comparison.group_b.sample_count >= MIN_SAMPLES_FOR_COMPARISON
        # 措辞必须是观察性的。这里直接查那几个词，因为它是本层最容易被
        # "顺手润色"掉的一句话。
        assert "更好" not in comparison.statement, comparison.statement
        assert "优于" not in comparison.statement, comparison.statement
        assert "在当前样本中" in comparison.statement, comparison.statement

    # 放行时证据全部是比较证据；每条都可追溯、全都没有 confidence。
    kinds = {e.type for e in report.evidence}
    assert kinds == {LearningEvidenceKind.COHORT_DIFFERENCE}, kinds
    for evidence in report.evidence:
        assert evidence.confidence is None
        assert evidence.source_snapshot_ids and evidence.source_post_ids
        assert evidence.source_x_post_ids, "证据回不到 x_post_id"
        assert evidence.sample_count >= MIN_SAMPLES_FOR_COMPARISON

    return {
        "available": True,
        "data": "合成",
        "post_count": report.post_count,
        "window": report.observation_window,
        "cohort_count": len(report.cohorts),
        "valid_comparison_count": len(valid),
        "evidence_count": len(report.evidence),
        "evidence_kinds": sorted(k.value for k in kinds),
        "example_statement": valid[0].statement,
        "example_traceability": {
            "snapshot_ids": list(report.evidence[0].source_snapshot_ids),
            "post_ids": list(report.evidence[0].source_post_ids),
            "x_post_ids": list(report.evidence[0].source_x_post_ids),
        },
        "readiness": _readiness_summary(report.readiness),
        "no_apply_learning": _no_apply_learning(),
        "note": (
            "闸门放行只表示'可以作为未来学习材料'；automatic_modification 仍为 False，"
            "而且学习层里没有 apply_learning()。"
        ),
    }


# ---------------------------------------------------------------------------
# Smoke 4：缺 metrics → gate false（不许补 0）
# ---------------------------------------------------------------------------


def smoke_4_missing_metrics() -> dict:
    """样本够 5 条帖子，但三类计数各自都没到 5 条读数 → `missing_metrics`。

    造法是让每条帖子**只有一类**计数有读数（2 / 2 / 1）：

    ```text
    attention     n=2   （post-01、post-02 有 likes）
    conversation  n=2   （post-03、post-04 有 replies+quotes）
    amplification n=1   （post-05 有 reposts）
    ```

    五条帖子都不是空的（各自至少一类有读数），所以 `sample_count=5` 过关；
    三类里最多的也只有 2 条，所以 `metrics_present` 不过关 → 闸门给
    `missing_metrics`，**不是** `insufficient_sample`。两条原因必须能分辨，
    否则"读不到"与"太少"会共用一句解释。
    """
    records = [
        record(1, hook="question", likes=10),
        record(2, hook="question", likes=11),
        record(3, hook="statement", replies=3, quotes=1),
        record(4, hook="statement", replies=2, quotes=0),
        record(5, hook="statement", reposts=5),
    ]

    report = build_learning_report_offline(records)

    assert report.readiness.learning_ready is False
    assert report.readiness.reason == "missing_metrics", report.readiness.reason
    assert report.readiness.sample_count == 5, report.readiness.sample_count
    assert report.readiness.automatic_modification is False

    # 缺失**没有**被补成 0：post-03 没有 likes，它的 attention 必须是 None。
    # 补成 0 会让它落进 attention 的分布下沿，变成一条"这类帖子更差"的假结论。
    by_id = {r.published_post_id: r for r in records}
    assert by_id["post-03"].performance.attention is None
    assert by_id["post-01"].performance.conversation is None
    assert by_id["post-05"].performance.attention is None

    # 不成立也要摆出来：报告里必须有一条"什么也没法说"的证据。
    assert len(report.evidence) == 1, [e.type for e in report.evidence]
    status = report.evidence[0]
    assert status.type is LearningEvidenceKind.MISSING_METRIC, status.type
    assert status.confidence is None and status.comparison_group is None
    assert "不按 0 计入" in status.statement, status.statement

    metrics_check = next(c for c in report.readiness.checks if c.name == "metrics_present")
    assert metrics_check.passed is False
    assert "attention=2" in metrics_check.detail, metrics_check.detail

    return {
        "available": True,
        "data": "合成",
        "post_count": report.post_count,
        "window": report.observation_window,
        "readings_by_metric": {"attention": 2, "conversation": 2, "amplification": 1},
        "missing_attention_is_none": by_id["post-03"].performance.attention is None,
        "status_evidence_statement": status.statement,
        "readiness": _readiness_summary(report.readiness),
        "note": (
            "样本量够（5 条帖子）但三类读数都不足 → missing_metrics。"
            "缺失一律 None，不进任何分布，也不按 0 计入。"
        ),
    }


# ---------------------------------------------------------------------------
# Smoke 5：不同 observation window → gate false
# ---------------------------------------------------------------------------


def smoke_5_window_mismatch() -> dict:
    """同一批记录里混了两个窗口 → `inconsistent_window`，不比。

    工作单第十六节：发布后 2 小时的计数与发布后 48 小时的计数不是同一件事。
    这里把两组各 5 条摆在一起并**显式要求**按 `24-72h` 报告——闸门必须先拒绝
    整份报告，而不是悄悄只留下符合窗口的那一半然后照常比较。
    """
    records = [
        record(i, hook="question", likes=10 + i, window=WINDOW) for i in range(1, 6)
    ] + [record(i, hook="statement", likes=30 + i, window=OTHER_WINDOW) for i in range(6, 11)]

    report = build_learning_report(records, window=WINDOW)

    assert report.readiness.learning_ready is False
    assert report.readiness.reason == "inconsistent_window", report.readiness.reason
    assert report.readiness.automatic_modification is False

    window_check = next(c for c in report.readiness.checks if c.name == "window_consistent")
    assert window_check.passed is False, window_check.detail

    # 合格的那一半仍然算进样本量（分析单位是帖子，且窗口对得上）；
    # 不合格的那一半**不进任何 cohort**——一个自称 24-72h 的组里塞进
    # 一条 1-6h 的帖子，会让"这个组是 24-72h"这句话变成假的。
    assert report.readiness.sample_count == 5, report.readiness.sample_count
    for cohort in report.cohorts:
        assert cohort.observation_window == WINDOW, cohort.observation_window
        assert cohort.sample_count == 5, cohort.sample_count
        for post_id in cohort.post_ids:
            assert int(post_id.split("-")[1]) <= 5, post_id

    assert not report.valid_comparisons, "窗口不一致却产出了成立的比较"
    assert len(report.evidence) == 1
    status = report.evidence[0]
    assert status.type is LearningEvidenceKind.WINDOW_MISMATCH, status.type
    assert "窗口不一致时不直接比较" in status.statement, status.statement

    return {
        "available": True,
        "data": "合成",
        "windows_in_records": [WINDOW, OTHER_WINDOW],
        "requested_window": WINDOW,
        "sample_count_in_window": report.readiness.sample_count,
        "cohort_count": len(report.cohorts),
        "valid_comparison_count": len(report.valid_comparisons),
        "status_evidence_statement": status.statement,
        "readiness": _readiness_summary(report.readiness),
        "note": (
            "两个窗口的记录混在一起时，闸门的 window_consistent 先拒绝整份报告；"
            "报告里不会出现任何跨窗口的差异结论。"
        ),
    }


# ---------------------------------------------------------------------------
# Smoke 1 / 2：真机库（只读）
# ---------------------------------------------------------------------------


async def smoke_1_real_post_chain(runtime) -> dict:
    """真实已发布帖子 → 快照 → performance observation，逐段核对。

    核对的是**链子**，不是数值：帖子数、快照数、`hours_since_publish` 分到哪一档、
    窗口取值取到的是不是窗口内最新那条。数值本身（两个 0）没有任何可解释性，
    断言它们等于多少只会把一次真实的观察写死成一个期望值。
    """
    async with runtime.session_factory() as session:
        records, window, assembly_limitations = await load_records(session, runtime.graph)
        # 用**学习层自己**那个读法取帖子：status=published 且带 x_post_id /
        # published_at。换成"所有 published_post 行"会让这里数出一些学习层
        # 看不到的帖子，于是 Smoke 1 的帖子数与 Smoke 2 的对不上。
        post_rows = await repositories.list_published_posts_with_x_id(session)

    if not post_rows:
        return {
            "available": False,
            "blocked": True,
            "reason": (
                "库里没有任何 status=published 且带 x_post_id / published_at 的帖子，"
                "这条数据链**没有可跑的对象**。"
                "工作单第三十二节：数据不足时不编，如实记 blocked。"
            ),
        }

    # 逐条重读一遍归因，把"记录是搬运来的"这件事显式做一遍，
    # 而不是相信 `load_records` 没出错。
    async with runtime.session_factory() as session:
        profiles = [
            await build_post_performance_profile(session, runtime.graph, published_post_id=row.id)
            for row in post_rows
        ]
    profiles = [p for p in profiles if p is not None]
    assert profiles, "列出了帖子却一条归因都读不到"

    chain: list[dict] = []
    async with runtime.session_factory() as session:
        for profile in profiles:
            # 直接再查一次快照表，把"观察是从这些行搬来的"钉在数据库上——
            # 而不是相信 `build_post_performance_profile` 会自己搬运。
            rows = await repositories.list_post_metrics_snapshots(
                session, published_post_id=profile.published_post_id
            )
            assert [str(r.id) for r in rows] == [o.snapshot_id for o in profile.observations], (
                f"{profile.published_post_id} 的观察与快照行对不上"
            )
            assert [r.hours_since_publish for r in rows] == [
                o.hours_since_publish for o in profile.observations
            ], f"{profile.published_post_id} 的 hours_since_publish 没有原样搬运"

            rebuilt = record_from_profile(profile, window=window)
            assert rebuilt.observations == profile.observations, "观察序列没有原样搬运"
            selected = select_window_observation(profile.observations, window)
            assert rebuilt.performance == vector_at_window(profile.observations, window)
            if selected is None:
                # 窗口内没有观察 → 三个分量全 None。**不是 0**。
                assert rebuilt.performance.is_empty
            chain.append(
                {
                    "published_post_id": profile.published_post_id,
                    "x_post_id": profile.x_post_id,
                    "published_at": profile.published_at,
                    "snapshot_count": len(profile.observations),
                    "snapshot_ids": [o.snapshot_id for o in profile.observations],
                    "hours_since_publish": [o.hours_since_publish for o in profile.observations],
                    "windows": [
                        window_bucket(o.hours_since_publish) for o in profile.observations
                    ],
                    "in_window_snapshot_id": selected.snapshot_id if selected else None,
                    "performance": rebuilt.performance.model_dump(),
                    "hook_type": rebuilt.hook_type,
                    "structure_type": rebuilt.structure_type,
                    "stance": rebuilt.stance,
                    "content_length": rebuilt.content_length,
                }
            )

    snapshot_total = sum(entry["snapshot_count"] for entry in chain)
    assert snapshot_total >= len(profiles), "有帖子却一条快照都没连上"

    return {
        "available": True,
        "data": "真机库（只读）",
        "post_count": len(profiles),
        "snapshot_count": snapshot_total,
        "window": window,
        "assembly_limitations": list(assembly_limitations),
        "assembly_sample_count": len(records),
        "chain": chain,
        "note": (
            "归因链 published_post → post_metrics_snapshot → SnapshotObservation "
            "→ PerformanceVector 在真机上连通；本步只读。"
        ),
    }


async def smoke_2_real_readiness(runtime) -> dict:
    """真实当前数据 → `learning_ready=false`。

    理由必须具体。真机上是"只有 1 条帖子、2 次观察"——那是 `insufficient_sample`，
    而**不是**"没有数据"（`available` 仍然为 `True`）也不是"没有结论"
    （报告里有明确的一条状态证据）。工作单第三十七节要求这两者分得清。
    """
    async with runtime.session_factory() as session:
        records, window, _ = await load_records(session, runtime.graph)
        report = build_learning_report(records, window=window)

    readiness = report.readiness
    assert readiness.learning_ready is False, (
        "真机数据只有个位数帖子，闸门却放行了——这比一条假结论更严重"
    )
    assert readiness.automatic_modification is False
    assert tuple(c.name for c in readiness.checks) == CHECK_NAMES

    if report.post_count < MIN_SAMPLES_FOR_COMPARISON:
        assert readiness.reason == "insufficient_sample", readiness.reason
        assert readiness.sample_count == report.post_count

    assert len(report.evidence) == 1, [e.type for e in report.evidence]
    status = report.evidence[0]
    assert status.type is LearningEvidenceKind.INSUFFICIENT_SAMPLE, status.type
    assert status.confidence is None
    assert status.sample_count == readiness.sample_count

    return {
        "available": True,
        "data": "真机库（只读）",
        "post_count": report.post_count,
        "snapshot_total": sum(len(r.observations) for r in report.records),
        "window": report.observation_window,
        "valid_comparison_count": len(report.valid_comparisons),
        "status_evidence_statement": status.statement,
        "status_evidence_traceability": {
            "snapshot_ids": list(status.source_snapshot_ids),
            "post_ids": list(status.source_post_ids),
            "x_post_ids": list(status.source_x_post_ids),
        },
        "readiness": _readiness_summary(readiness),
        "no_apply_learning": _no_apply_learning(),
        "note": (
            "真实数据不足以产生任何比较结论，闸门给出 insufficient_sample；"
            "这是一条结论，不是一次失败。"
        ),
    }


# ---------------------------------------------------------------------------
# 只读 GET（best-effort）
# ---------------------------------------------------------------------------


async def live_get_readonly(x_post_id: str | None) -> dict:
    """向 X 回读一次那条真实帖子的公开计数。**只读，且失败不算错。**

    拿到的数字**不写库**——它只是"此刻只读这条路通不通"的证据。写进去就会多一条
    观察，而这条观察不是按采集节奏采的，它会让 `hours_since_publish` 的序列
    多出一个没人打算要的点。
    """
    if not x_post_id:
        return {
            "available": False,
            "blocked": True,
            "reason": "没有任何带 x_post_id 的已发布帖子可以回读。",
        }

    service = XService(RealXApiClient())
    try:
        observations, not_returned = await service.fetch_public_metrics(
            [x_post_id], observed_at=datetime.now(timezone.utc)
        )
    except Exception as exc:  # noqa: BLE001 - 外部依赖不可用不是本阶段的问题
        return {
            "available": False,
            "blocked": True,
            "reason": f"{type(exc).__name__}: {exc}",
            "note": "只读 GET 没成功（网络/凭据/代理）；这不是本阶段的失败，如实记录。",
        }

    return {
        "available": True,
        "data": "真实 X（只读 GET）",
        "requested_ids": [x_post_id],
        "returned": len(observations),
        "not_returned": list(not_returned),
        "counts": [
            {
                "x_post_id": o.platform_post_id,
                "likes": o.likes,
                "replies": o.replies,
                "reposts": o.reposts,
                "quotes": o.quote_count,
            }
            for o in observations
        ],
        "note": "这些数字**没有**写进数据库；本脚本不产生新的观察行。",
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _method_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in X_REQUESTS:
        counts[entry["method"]] = counts.get(entry["method"], 0) + 1
    return counts


async def run_() -> int:
    from app.config import settings

    url = settings.langgraph_checkpoint_database_url or settings.database_url
    report: dict = {
        "phase": "5C",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "工作单第三十八节五条 smoke 的记录。Smoke 1 / 2 读真机库（只读），"
            "Smoke 3 / 4 / 5 在合成记录上跑闸门。本脚本不写数据库、不写 checkpoint、"
            "不发布任何内容；对 X 只有一次 best-effort 只读 GET。"
        ),
        "smokes": {},
        "failed": {},
    }

    # --- 离线三条（不需要运行时） ---
    for key, fn in (
        ("3_offline_cohorts_gate_true", smoke_3_offline_cohorts),
        ("4_missing_metrics_gate_false", smoke_4_missing_metrics),
        ("5_window_mismatch_gate_false", smoke_5_window_mismatch),
    ):
        print(f">>> Smoke {key}", flush=True)
        try:
            report["smokes"][key] = fn()
            print("    通过。", flush=True)
        except Exception as exc:  # noqa: BLE001 - 报告优先，异常要有去处
            report["smokes"][key] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
            report["failed"][key] = f"{type(exc).__name__}: {exc}"
            print(f"    **失败了**：{exc}", flush=True)

    # --- 真机两条 + 一次只读 GET ---
    x_post_id: str | None = None
    async with create_checkpointer(url) as checkpointer:
        runtime = build_runtime(checkpointer)

        async with runtime.session_factory() as session:
            posts = await repositories.list_published_posts_with_x_id(session)
        x_post_id = posts[0].x_post_id if posts else None

        for key, fn in (
            ("1_real_post_observation_chain", smoke_1_real_post_chain),
            ("2_real_data_gate_false", smoke_2_real_readiness),
        ):
            print(f">>> Smoke {key}", flush=True)
            try:
                report["smokes"][key] = await fn(runtime)
                print("    通过。" if report["smokes"][key].get("available") else "    如实记 blocked。", flush=True)
            except Exception as exc:  # noqa: BLE001
                report["smokes"][key] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
                report["failed"][key] = f"{type(exc).__name__}: {exc}"
                print(f"    **失败了**：{exc}", flush=True)

    print(">>> X 只读 GET（best-effort）", flush=True)
    try:
        report["smokes"]["x_readonly_get"] = await live_get_readonly(x_post_id)
        print(
            "    通过。" if report["smokes"]["x_readonly_get"].get("available") else "    如实记 blocked。",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        report["smokes"]["x_readonly_get"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    post_tweets = sum(
        1 for r in X_REQUESTS if r["method"] != "GET" and r["path"].endswith("/2/tweets")
    )
    report["x_requests"] = {
        "total": len(X_REQUESTS),
        "by_method": _method_counts(),
        "post_tweets": post_tweets,
        "requests": X_REQUESTS,
    }
    assert post_tweets == 0, f"发出了 {post_tweets} 次 POST /2/tweets"

    print("\n--- 请求账（工作单第三十节） ---")
    print(f"总请求 {len(X_REQUESTS)}，按方法 {_method_counts()}")
    print(f"POST /2/tweets：{post_tweets}（必须是 0）")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    document: dict = {}
    if REPORT_PATH.exists():
        try:
            loaded = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
        except json.JSONDecodeError:
            document = {}
    document.setdefault("phase", "5C")
    document.setdefault("note", "每次 smoke 一份记录，累积保存在 runs 里。")
    document.setdefault("runs", []).append(report)
    REPORT_PATH.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\n记录已写入 {REPORT_PATH}")

    if report["failed"]:
        print(f"注意：{len(report['failed'])} 条 smoke 失败 —— {json.dumps(report['failed'], ensure_ascii=False)}")
        return 1
    return 0


def main() -> int:
    install_read_only_guard()
    return asyncio.run(run_())


if __name__ == "__main__":
    raise SystemExit(main())
