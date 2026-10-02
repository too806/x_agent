"""Phase 5D 的 smoke（工作单第三十六节）。

工作单列了五条，五条都在这里跑：

| Smoke | 看什么 | 数据 | 断言什么 |
|---|---|---|---|
| 1 | 真实当前数据 → 闸门关 → 零条提案 | 真机库（只读） | `learning_ready=false`、`proposal_count=0` |
| 2 | 离线 5+5 → 闸门开 | 合成 | `learning_ready=true` |
| 3 | 合法证据 → 产出提案 | 合成 | 至少一条，且它自己过全部判据 |
| 4 | 因果措辞的提案 → 不成立 | 合成（手写） | `validate_proposal` 判 invalid |
| 5 | 目标写成 Guardrail / Publish / Budget → 不成立 | 合成（手写） | 同上，且点名生产边界 |

Smoke 4 / 5 的输入是**手写的**提案，因为生成路径永远产不出这两种（这本身就是
Smoke 3 的结论）。手写是刻意的反证：证明那两道闸门真的会拦人，而不是"恰好
没生成过"。

## 这一份与前几阶段最大的不同

Smoke 1 要证明的**不是**"能提出建议"，而是"提不出来"——真机上是 1 条帖 /
2 次观察，正确的输出就是零条提案加一句原因。工作单第二十八、三十七节：
零条是一个**结论**，不是一次失败。把"零条"报成失败，会诱使下一个人去
发测试帖凑数据。

## 绝不发布，而且在发出去之前就拦

`RealXApiClient._request` 换成前几阶段同一套只读守卫：**非 GET 在发出去之前
就抛**。工作单第二十九节允许 GET、禁止 `POST /2/tweets`；报告末尾按方法
分组给出请求计数，`POST /2/tweets` 必须是 0。

## 一个字都不写

本脚本不写数据库、不写 checkpoint、不修改任何 metrics，也**不写裁决**。
它读到的每一样东西都原样出现在报告里。末尾会比对一次 `learning_proposal`
的行数：跑完之后必须还是 0。

运行：`python scripts/smoke_5d.py`（需 PostgreSQL 在跑、.env 有配置；
X 凭据可选——没有就只记 blocked）
"""

import asyncio
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
from app.feedback.signals import MIN_SAMPLES_FOR_COMPARISON  # noqa: E402
from app.learning.proposal import (  # noqa: E402
    ABSOLUTE_MARKERS,
    generate_proposals,
    validate_proposal,
)
from app.learning.report import build_learning_report, build_learning_report_offline  # noqa: E402
from app.learning.runtime import load_records  # noqa: E402
from app.learning.schema import (  # noqa: E402
    LearningProposal,
    PostPerformanceRecord,
    ProposalStatus,
)
from app.main import build_runtime  # noqa: E402
from app.services.admin import AdminService  # noqa: E402
from app.services.x_api import RealXApiClient  # noqa: E402
from app.services.x_service import XService  # noqa: E402
from app.style.recipe import build_recipe, recipe_version  # noqa: E402
from app.style.schemas import StyleProfileFeatures  # noqa: E402
from app.style.taxonomy import HookType, StructureType  # noqa: E402

REPORT_PATH = ROOT / "docs" / "phase-5d-smoke-runs.json"

#: 合成记录的起点。写死而不是 `now()`：报告要能被复算。
PUBLISHED_AT = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
WINDOW = "24-72h"
HOURS_IN_WINDOW = 48.0

#: 对 X 发出的每一次请求（方法与路径）。报告末尾据此证明"没有写请求"。
X_REQUESTS: list[dict[str, str]] = []


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
            raise AssertionError(f"Phase 5D smoke 只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------


def _synthetic_x_id(index: int) -> str:
    """合成帖子的 x_post_id：与真实 id 同形状、不同段，报告里一眼可辨。"""
    return f"9000000000000000{index:03d}"


def record(
    index: int,
    *,
    hook: str,
    likes: int | None = None,
    replies: int | None = None,
    reposts: int | None = None,
    window: str = WINDOW,
) -> PostPerformanceRecord:
    """一条合成记录：一条帖子、一个窗口、一条观察。没有一处 `or 0`。"""
    from app.feedback.schema import SnapshotObservation
    from app.learning.performance import vector_at_window

    observation = SnapshotObservation(
        snapshot_id=f"snap-{index:02d}",
        x_post_id=_synthetic_x_id(index),
        published_post_id=f"post-{index:02d}",
        observed_at=PUBLISHED_AT + timedelta(hours=HOURS_IN_WINDOW),
        hours_since_publish=HOURS_IN_WINDOW,
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=0,
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


def balanced_records() -> list[PostPerformanceRecord]:
    """三指标**同向**的 5 + 5。

    必须同向：本层只在三类活动计数方向一致时才提案（见
    `app/learning/proposal.py::_build_for_target`）。只让点赞不同的话，
    生成路径会正确地拒绝——那会让 Smoke 3 变成一条关于"代码没生效"的假失败。
    """
    return [
        record(i, hook=HookType.QUESTION.value, likes=20, replies=12, reposts=6)
        for i in range(1, 6)
    ] + [
        record(i, hook=HookType.STATEMENT.value, likes=8, replies=4, reposts=1)
        for i in range(6, 11)
    ]


def base_recipe():
    """基准配方：开头方式取 statement（观察上**较低**的那一侧）。

    刻意不取 question：配方若已经就在更高的一侧，生成路径会正确地判定
    "没有可尝试的变化"而不产出提案，Smoke 3 就无从谈起。
    """
    profile = StyleProfileFeatures(
        sample_count=40,
        median_length=180.0,
        number_density=1.0,
        emoji_density=0.1,
        structure_distribution={StructureType.OPINION_FIRST.value: 0.6},
        hook_distribution={HookType.STATEMENT.value: 0.7, HookType.QUESTION.value: 0.3},
        opening_distribution={},
        closing_distribution={HookType.STATEMENT.value: 0.7},
        sentence_length_distribution={},
    )
    return build_recipe(profile)


def handcrafted_proposal(**overrides) -> LearningProposal:
    """一条**合法**的手写提案，供 Smoke 4 / 5 改一处来反证闸门会拦。"""
    base = dict(
        id="proposal:hook_type@24-72h",
        target="style_recipe.hook_type",
        current_value=HookType.STATEMENT.value,
        proposed_value=HookType.QUESTION.value,
        rationale=(
            "在 24-72h 观察窗口下，按开头方式分组，question（n=5）的点赞中位数为 20，"
            "statement（n=5）为 8。在本样本中观察到「question」一侧更高。"
            "这是观察到的差异，不构成因果结论；它只说明在下一步里值得尝试把"
            "「开头方式」取 question（当前为 statement）再观察一次。"
        ),
        evidence_ids=("learning:hook-type:question:statement:24-72h:attention",),
        sample_count=10,
        observation_window=WINDOW,
        comparison_group="hook_type: question vs statement @ 24-72h · attention",
        limitations=("这是一次观察性比较，没有对照组。",),
        status=ProposalStatus.PROPOSED,
        current_recipe_version=recipe_version(base_recipe()),
        proposed_recipe_version="sr1-000000000000",
        outlier_count=0,
        automatic_application=False,
    )
    base.update(overrides)
    return LearningProposal(**base)


def _proposal_summary(proposal: LearningProposal) -> dict:
    """一条提案的对外形状。**不含** proposal_score / confidence 之外的自造字段。"""
    validation = validate_proposal(proposal)
    return {
        "id": proposal.id,
        "target": proposal.target,
        "current_value": proposal.current_value,
        "proposed_value": proposal.proposed_value,
        "rationale": proposal.rationale,
        "evidence_ids": list(proposal.evidence_ids),
        "sample_count": proposal.sample_count,
        "observation_window": proposal.observation_window,
        "comparison_group": proposal.comparison_group,
        "confidence": proposal.confidence,
        "limitations": list(proposal.limitations),
        "status": proposal.status.value,
        "current_recipe_version": proposal.current_recipe_version,
        "proposed_recipe_version": proposal.proposed_recipe_version,
        "outlier_count": proposal.outlier_count,
        "automatic_application": proposal.automatic_application,
        "validation": {
            "valid": validation.valid,
            "reasons": list(validation.reasons),
            "passed_checks": list(validation.passed_checks),
        },
    }


# ---------------------------------------------------------------------------
# Smoke 2 / 3：离线 5+5 → 闸门开 → 产出提案
# ---------------------------------------------------------------------------


def smoke_2_offline_gate_open() -> dict:
    """工作单第三十六节第 2 条：离线 5+5 → `learning_ready=true`。

    闸门放行**不**等于可以改自己，所以这条同时钉住
    `automatic_modification=false`。两件事必须能分开看。
    """
    records = balanced_records()
    report = build_learning_report_offline(records)

    assert report.readiness.learning_ready is True, report.readiness
    assert report.readiness.reason == "ready", report.readiness.reason
    assert report.readiness.automatic_modification is False
    assert report.readiness.sample_count == MIN_SAMPLES_FOR_COMPARISON * 2
    assert report.valid_comparisons, "两组各 5 条却没有一条比较成立"

    return {
        "available": True,
        "data": "合成",
        "post_count": report.post_count,
        "window": report.observation_window,
        "valid_comparison_count": len(report.valid_comparisons),
        "readiness": {
            "learning_ready": report.readiness.learning_ready,
            "reason": report.readiness.reason,
            "sample_count": report.readiness.sample_count,
            "automatic_modification": report.readiness.automatic_modification,
        },
        "note": "样本充足时闸门放行；automatic_modification 仍为 False。",
    }


def smoke_3_proposal_is_generated() -> dict:
    """工作单第三十六节第 3 条：合法证据 → 产出提案，且它自己过全部判据。"""
    report = build_learning_report_offline(balanced_records())
    proposals = generate_proposals(report, base_recipe=base_recipe())

    assert proposals, "闸门开着、比较成立，却没有产出任何提案"
    for proposal in proposals:
        validation = validate_proposal(proposal, report)
        assert validation.valid, (proposal.id, validation.reasons)
        assert proposal.confidence is None, "本仓库没有校准过的概率模型"
        assert proposal.automatic_application is False
        assert proposal.evidence_ids, "提案必须带证据 id"
        assert proposal.status is ProposalStatus.PROPOSED

    proposal = proposals[0]
    # 样本量是**帖子并集**（10 条），不是三条指标相加的 30。
    assert proposal.sample_count == len(balanced_records()), proposal.sample_count
    assert len(proposal.evidence_ids) > 1, "三指标的观察应当归到同一条提案里"
    assert proposal.sample_count < len(proposal.evidence_ids) * proposal.sample_count

    return {
        "available": True,
        "data": "合成",
        "proposal_count": len(proposals),
        "proposals": [_proposal_summary(item) for item in proposals],
        "note": (
            "同维度的多条比较归成**一条**提案；样本量取帖子并集而不是相加。"
            "验证：生成路径产出的提案全部 valid。"
        ),
    }


# ---------------------------------------------------------------------------
# Smoke 4 / 5：手写反证，证明两道闸门真的会拦
# ---------------------------------------------------------------------------


def smoke_4_causal_wording_is_rejected() -> dict:
    """工作单第三十六节第 4 条：因果措辞的提案 → 不成立。

    底稿本身合法（先验一遍），只改措辞。否则"被拒"可能只是底稿本来就有问题。
    """
    assert validate_proposal(handcrafted_proposal()).valid, "底稿本身就不合法"

    causal = handcrafted_proposal(
        rationale="因为用了问句开头，所以点赞更多；这证明该把开头方式改成 question。"
    )
    validation = validate_proposal(causal)
    assert validation.valid is False, "因果措辞的提案被判成了 valid"

    absolute = handcrafted_proposal(
        rationale="改成 question 一定能提高互动，这一点是必然的。"
    )
    absolute_validation = validate_proposal(absolute)
    assert absolute_validation.valid is False, "绝对化措辞的提案被判成了 valid"

    instructional = handcrafted_proposal(
        rationale="你应该把开头方式改成 question，请照这个方向调整。"
    )
    instructional_validation = validate_proposal(instructional)
    assert instructional_validation.valid is False, "指令措辞的提案被判成了 valid"

    return {
        "available": True,
        "data": "合成（手写）",
        "template_valid": True,
        "causal_reasons": list(validation.reasons),
        "absolute_reasons": list(absolute_validation.reasons),
        "instructional_reasons": list(instructional_validation.reasons),
        "causal_markers": list(ABSOLUTE_MARKERS),
        "note": (
            "底稿合法，只改措辞 → 三种都被拒。生成路径产不出它们，"
            "所以必须手写才能证明这道闸门不是摆设。"
        ),
    }


def smoke_5_production_targets_are_rejected() -> dict:
    """工作单第三十六节第 5 条：目标写成 Guardrail / Publish / Budget → 不成立。

    除了"被拒"，还要证明拒绝**点出了它踩的是哪条生产边界**——只回一句
    "不合法"的话，读的人会以为是自己写错了语法。
    """
    targets = [
        "guardrail.max_scope_exceeds_evidence",
        "publish.schedule",
        "publish.auto_publish",
        "budget.daily_limit",
        "critic.threshold",
        "trend.weight_recency",
    ]
    results: list[dict] = []
    for target in targets:
        validation = validate_proposal(handcrafted_proposal(target=target))
        assert validation.valid is False, f"{target} 被当成了合法目标"
        joined = " ".join(validation.reasons)
        assert "安全或生产边界" in joined, (target, validation.reasons)
        results.append(
            {"target": target, "valid": False, "reasons": list(validation.reasons)}
        )

    # 反证的另一半：允许的目标照常成立，否则"全拒"也可能只是恒真。
    allowed = validate_proposal(handcrafted_proposal(target="style_recipe.hook_type"))
    assert allowed.valid is True, allowed.reasons

    return {
        "available": True,
        "data": "合成（手写）",
        "rejected": results,
        "allowed_target_still_valid": True,
        "note": (
            "六个生产目标全部被拒且点名边界；作为对照，允许的目标仍然成立。"
        ),
    }


# ---------------------------------------------------------------------------
# Smoke 1：真机库（只读）
# ---------------------------------------------------------------------------


async def smoke_1_real_data_gate_closed(runtime) -> dict:
    """工作单第三十六节第 1 条：真实当前数据 → 闸门关 → **零条提案**。

    这条同时走**两条路**：直接调生成函数，以及调控制台真正用的那个服务方法。
    两条都必须是零条——只验一条的话，服务层某天多绕一道弯就可能把提案放出来。
    """
    service = AdminService(session_factory=runtime.session_factory, graph=runtime.graph)

    async with runtime.session_factory() as session:
        records, window, _ = await load_records(session, runtime.graph)

    snapshot_total = sum(len(item.observations) for item in records)
    report = build_learning_report(records, window=window)
    proposals = generate_proposals(report, base_recipe=None)

    overview = await service.learning()
    proposal_overview = await service.learning_proposals()

    assert overview.proposal_count == 0, (
        f"真机数据只有 {report.post_count} 条帖子，却算出了 {overview.proposal_count} 条提案"
    )
    assert proposal_overview.proposal_count == 0, proposal_overview.proposal_count
    assert proposals == (), [p.id for p in proposals]
    assert proposal_overview.proposals == [], "闸门关着却列出了提案"
    assert proposal_overview.learning_ready is False
    assert report.readiness.automatic_modification is False

    if report.post_count < MIN_SAMPLES_FOR_COMPARISON:
        assert report.readiness.reason == "insufficient_sample", report.readiness.reason

    # 闸门关着时**连写路径也够不到**：任何 id 都不能被裁决。
    review_refused = await _review_is_refused(service)

    return {
        "available": True,
        "data": "真机库（只读）",
        "post_count": report.post_count,
        "snapshot_count": snapshot_total,
        "window": report.observation_window,
        "learning_ready": report.readiness.learning_ready,
        "reason": report.readiness.reason,
        "sample_count": report.readiness.sample_count,
        "cohort_count": len(report.cohorts),
        "valid_comparison_count": len(report.valid_comparisons),
        "proposal_count_from_report": len(proposals),
        "proposal_count_from_service": overview.proposal_count,
        "proposals_overview": {
            "available": proposal_overview.available,
            "learning_ready": proposal_overview.learning_ready,
            "reason": proposal_overview.reason,
            "proposal_count": proposal_overview.proposal_count,
            "reviewable_count": proposal_overview.reviewable_count,
            "decided_count": proposal_overview.decided_count,
        },
        "review_refused_when_gate_closed": review_refused,
        "note": (
            "真实数据不足以支撑任何比较，所以正确的输出是零条提案加一句原因。"
            "工作单第二十八、三十七节：零条是一个结论，不是一次失败。"
        ),
    }


async def _review_is_refused(service: AdminService) -> dict:
    """闸门关着时，写端点对任何 id 都应当拒绝，而且**一行都不写**。

    用一条**真的会生成**的 id（如果哪天数据够了它就会出现）去试，避免
    "因为 id 拼错所以 404"这种什么都证明不了的通过。
    """
    from app.services.admin import LearningProposalReviewError

    probe = "proposal:hook_type@24-72h"
    try:
        await service.review_learning_proposal(probe, action="approve")
    except LearningProposalReviewError as exc:
        return {"probe_id": probe, "refused": True, "reason": exc.reason, "detail": exc.detail}
    return {"probe_id": probe, "refused": False, "detail": "写端点接受了这条提案"}


# ---------------------------------------------------------------------------
# 只读 GET（best-effort）与请求账
# ---------------------------------------------------------------------------


async def live_get_readonly(x_post_id: str | None) -> dict:
    """向 X 回读一次那条真实帖子的公开计数。**只读，且失败不算错。**

    拿到的数字**不写库**——写进去就会多出一条不是按采集节奏采的观察。
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


def _method_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in X_REQUESTS:
        counts[entry["method"]] = counts.get(entry["method"], 0) + 1
    return counts


async def _decision_row_count(runtime) -> int:
    async with runtime.session_factory() as session:
        return await repositories.count_learning_proposal_decisions(session)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


async def run_() -> int:
    from app.config import settings

    url = settings.langgraph_checkpoint_database_url or settings.database_url
    report: dict = {
        "phase": "5D",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "工作单第三十六节五条 smoke 的记录。Smoke 1 读真机库（只读），"
            "Smoke 2 / 3 在合成记录上跑生成路径，Smoke 4 / 5 用手写提案反证两道闸门。"
            "本脚本不写数据库、不写 checkpoint、不写裁决、不发布任何内容；"
            "对 X 只有一次 best-effort 只读 GET。"
        ),
        "smokes": {},
        "failed": {},
    }

    # --- 离线三条（不需要运行时） ---
    for key, fn in (
        ("2_offline_gate_open", smoke_2_offline_gate_open),
        ("3_proposal_is_generated", smoke_3_proposal_is_generated),
        ("4_causal_wording_rejected", smoke_4_causal_wording_is_rejected),
        ("5_production_targets_rejected", smoke_5_production_targets_are_rejected),
    ):
        print(f">>> Smoke {key}", flush=True)
        try:
            report["smokes"][key] = fn()
            print("    通过。", flush=True)
        except Exception as exc:  # noqa: BLE001 - 报告优先，异常要有去处
            report["smokes"][key] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
            report["failed"][key] = f"{type(exc).__name__}: {exc}"
            print(f"    **失败了**：{exc}", flush=True)

    # --- 真机一条 + 一次只读 GET ---
    x_post_id: str | None = None
    async with create_checkpointer(url) as checkpointer:
        runtime = build_runtime(checkpointer)

        before = await _decision_row_count(runtime)

        async with runtime.session_factory() as session:
            posts = await repositories.list_published_posts_with_x_id(session)
        x_post_id = posts[0].x_post_id if posts else None

        print(">>> Smoke 1_real_data_gate_closed", flush=True)
        try:
            report["smokes"]["1_real_data_gate_closed"] = await smoke_1_real_data_gate_closed(runtime)
            print("    通过。", flush=True)
        except Exception as exc:  # noqa: BLE001
            report["smokes"]["1_real_data_gate_closed"] = {
                "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
            report["failed"]["1_real_data_gate_closed"] = f"{type(exc).__name__}: {exc}"
            print(f"    **失败了**：{exc}", flush=True)

        after = await _decision_row_count(runtime)
        report["decision_rows"] = {
            "before": before,
            "after": after,
            "written_by_this_smoke": after - before,
        }
        assert after == before, f"smoke 写了 {after - before} 行裁决——它必须是只读的"

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

    report["automatic_modification"] = False
    report["automatic_application"] = False

    print("\n--- 请求账（工作单第二十九节） ---")
    print(f"总请求 {len(X_REQUESTS)}，按方法 {_method_counts()}")
    print(f"POST /2/tweets：{post_tweets}（必须是 0）")
    print(
        f"learning_proposal 行数：{report['decision_rows']['before']} → "
        f"{report['decision_rows']['after']}（本脚本写 0 行）"
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    document: dict = {}
    if REPORT_PATH.exists():
        try:
            loaded = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
        except json.JSONDecodeError:
            document = {}
    document.setdefault("phase", "5D")
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
