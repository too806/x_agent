"""Phase 5B.3 的真机验收 smoke（工作单第三十三、三十四节）。

五次 smoke，判定规则一条都不加——这里只**观察与记录**：

| Smoke | 看什么 |
|---|---|
| 1 | 正常热点：Trend → Evidence → 多个候选观点 → 确定性评估 → 选一个 |
| 2 | 证据冲突：`counterpoints` 与保守立场确实出现 |
| 3 | 证据不足：Agent **不**强行给出强观点 |
| 4 | 有新判断空间：`novelty` / `specificity` 彼此有区分度，且与证据支撑度分开 |
| 5 | 全链路：Viewpoint → StyleRecipe → Writer → Critic → `waiting_for_review` |

## 三次真实运行，五个观察面

Smoke 1 / 4 / 5 观察的是**同一次**运行（热点目标）的三个面：评估明细、分数的
区分度、链路完整性。它们不是三次实验，所以不该跑三遍——分开跑唯一的后果是
引入一个不必要的变量（每次检索回来的帖子都不一样），然后让三份报告看起来像
在互相印证，实际上各说各话。报告里每条都带 `source_run`，写明它观察的是哪一次。

Smoke 2（争议目标）与 Smoke 3（冷门目标）各跑一次，因为这两条要问的是**搜索
结果长什么样**，换目标就是换问题。

## 真实数据做不到的事，如实说做不到

Smoke 2 要的是"证据里真的有反方"，Smoke 3 要的是"证据真的不够"。这两件事取决于
当天 X 上有什么，**脚本强制不了**。所以每条 smoke 都分开记两样东西：

- **观察**：这次真机上实际出现了什么（可能没出现）；
- **确定性对照**：把这一轮**真实**的证据索引喂给生产的打分函数，验证
  "证据不足时上界推不到 strong""声明超范围会被压回"这两条**策略**在真实输入上
  确实成立。

只记前一半的话，一次恰好没有反方的运行会报告成"smoke 通过"，而那个性质
**根本没被检验到**——它看起来和检过了完全一样。

## 绝不发布

与 `scripts/smoke_5b1.py:96-121` / `scripts/smoke_5b2.py` 同一套只读守卫：
`RealXApiClient._request` 被替换，非 GET **在发出去之前**就抛。报告末尾给出
请求总数、按方法的分组，以及 `POST /2/tweets` 与 `POST /2/media` 各自的计数
（工作单第三十四节：两者都必须是 0）。

运行：`python scripts/smoke_5b3.py`（需 PostgreSQL 在跑、.env 有 X 与 LLM 凭据）
"""

import asyncio
import json
import sys
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path

# 与 smoke_5b1 / 5b2 同一个理由：仓库根必须排在 sys.path 最前，否则 `import app`
# 可能静默拿到 editable 安装指向的另一个检出。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agent import metrics, viewpoint  # noqa: E402
from app.agent.prompts import format_scope_signals, format_viewpoint  # noqa: E402
from app.agent.schemas import AngleDraft, ViewpointStance  # noqa: E402
from app.db.checkpointer import create_checkpointer  # noqa: E402
from app.main import build_runtime  # noqa: E402
from app.models import AgentRun  # noqa: E402
from app.repositories import create_agent_run  # noqa: E402
from app.services.x_api import RealXApiClient  # noqa: E402

REPORT_PATH = ROOT / "docs" / "phase-5b3-smoke-runs.json"

#: 三个目标就是三个不同的问题。它们**不是**同一问题的三种说法：
#: "值得讨论"问的是有没有判断空间，"有争议"问的是证据里有没有反方，
#: "冷门"问的是证据够不够。目标写错会让对应的 smoke 问不出该问的东西。
#: 三个目标都刻意写成**没有 Markdown、没有半角标点**的平实句子。它们会被
#: 一路兜底成 X 的检索词（候选选题不可检索时），而 X 的查询语法里
#: `* | ( ) / :` 各有含义——目标里写一个 `**重点**` 就足以让整次运行停在 400。
#: 这不是洁癖：真机 smoke 就是这么挂过一次的（见 `app/agent/search_query.py`）。
GOALS: dict[str, str] = {
    "hotspot": "今天帮我找一个最近值得在中文科技 X 圈讨论的 AI Agent 话题。",
    "conflict": (
        "帮我找一个最近中文科技圈在吵的 AI Agent 话题。"
        "有人夸也有人骂，双方都举得出具体例子。"
    ),
    "thin": (
        "帮我找一个冷门的 AI Agent 话题。"
        "某个小众工具的一个很具体的细节，最近几乎没人在讨论。"
    ),
}

X_REQUESTS: list[dict[str, str]] = []


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
            raise AssertionError(f"Phase 5B.3 smoke 只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 比较：与 RULE-023 同一套口径
# ---------------------------------------------------------------------------

#: `ViewpointScore` 上**每一个**由打分函数算出来的字段。`claim` 是输入不是
#: 输出，`reasons` 是自由文本（措辞改了不该算"分数变了"），两者除外。
#:
#: 用 `dc_fields` 取而不是手写一份：手写的那份会在有人给 `ViewpointScore`
#: 加字段时静静地漏掉新字段——而"少比一项就是给那一项留了一条可以悄悄变的路"
#: （`evals/behavior/rules.py::rule_023`）。
SCORE_FIELDS: tuple[str, ...] = tuple(
    f.name for f in dc_fields(viewpoint.ViewpointScore) if f.name not in ("claim", "reasons")
)


def _same(left, right) -> bool:
    """两个值是否算一样。浮点给容差，序列逐项比。

    序列必须逐项比而不能直接 `!=`：存下来的那一份过了 pydantic（`list`），
    重算的那一份是打分函数的原样返回值（`tuple`）。`["时代"] != ("时代",)`
    在 Python 里成立，于是"一个字都不差"会被报成不可复现。
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 1e-6
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
            return True
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _stored_value(angle, field: str):
    """存下来的 `Angle` 上，对应 `ViewpointScore.<field>` 的那个值。

    先看 Angle 自己（`viewpoint_score` / `confidence` / `stance` / 范围那几项
    直接在上面），再看它的 `assessment`（分项与计数在那里）。
    这个顺序不能反：`Angle.confidence` 是**生效值**（已被上限钳过），
    而 `assessment` 上没有 `confidence`——正因为它没有，才必须从 Angle 取。
    """
    if hasattr(angle, field):
        return getattr(angle, field)
    assessment = getattr(angle, "assessment", None)
    return getattr(assessment, field, None) if assessment is not None else None


def score_replay(state) -> dict:
    """用 state 里的原始输入把分数**重算一遍**，逐字段与存下来的比。

    这正是 RULE-023 在离线数据上做的事，只是换成真机产物。它回答的是工作单
    第十二节那个明确的反例——"这个观点 9.4 分，但无法复算"——有没有被真的修掉。

    重算的输入是 `Angle` 本身（它带着 claim / evidence_ids / 反方 / 申报 /
    `stance_declared`），所以这一步同时也在验证**打分函数对自己的输出幂等**：
    任何在"草稿 → Angle"转换中被丢掉的字段，都会在这里以"复算不出来"的形式暴露。
    """
    pack = pack_of(state)
    angles = list(state.get("angles") or [])
    if not angles:
        return {"available": False, "reason": "这一轮没有候选观点"}

    index = viewpoint.evidence_index(
        pack,
        state.get("research_item_ids") or [],
        state.get("research_results") or [],
    )
    fresh = viewpoint.score_viewpoints(angles, index, total_facts=len(index.fact_ids))

    mismatches: list[dict] = []
    skipped: list[str] = []
    for angle, score in zip(angles, fresh):
        if getattr(angle, "assessment", None) is None:
            # 老 checkpoint 的 Angle 上没有评估明细，无从比对。记下来，
            # 不当作通过——"没比"与"比了、一样"是两件事。
            skipped.append(angle.angle_id or angle.claim[:20])
            continue
        for field_name in SCORE_FIELDS:
            stored = _stored_value(angle, field_name)
            recomputed = getattr(score, field_name)
            if not _same(stored, recomputed):
                mismatches.append(
                    {
                        "angle_id": angle.angle_id,
                        "field": field_name,
                        "stored": _plain(stored),
                        "recomputed": _plain(recomputed),
                    }
                )

    return {
        "available": True,
        "angles_compared": len(angles) - len(skipped),
        "fields_per_angle": len(SCORE_FIELDS),
        "skipped_without_assessment": skipped,
        "mismatches": mismatches,
        "reproducible": not mismatches and not skipped,
    }


def _plain(value):
    """把枚举 / tuple 变成能进 JSON 的形态。"""
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return getattr(value, "value", value)


# ---------------------------------------------------------------------------
# 一个观点的确定性明细
# ---------------------------------------------------------------------------


def angle_detail(angle) -> dict:
    """一个 `Angle` 上**可复算**的那部分，给报告用。

    不含 `reasons`：那是给人读的推导说明，会随措辞改动而变，把它写进报告会让
    两份 diff 看起来很不一样而实际上算法没动。分项与计数已经足够复核。
    """
    assessment = getattr(angle, "assessment", None)
    detail: dict = {
        "angle_id": angle.angle_id,
        "claim": angle.claim,
        "evidence_ids": list(angle.evidence_ids),
        "counterpoints": list(getattr(angle, "counterpoints", []) or []),
        "counterpoint_evidence_ids": list(
            getattr(angle, "counterpoint_evidence_ids", []) or []
        ),
        "has_reasoning": bool((getattr(angle, "reasoning", "") or "").strip()),
        "risk": getattr(angle, "risk", ""),
        "confidence": angle.confidence,
        "viewpoint_score": angle.viewpoint_score,
        "stance": _plain(getattr(angle, "stance", None)),
        "stance_declared": _plain(getattr(angle, "stance_declared", None)),
        "max_stance": _plain(getattr(angle, "max_stance", None)),
        "claim_scope": _plain(getattr(angle, "claim_scope", None)),
        "evidence_scope": _plain(getattr(angle, "evidence_scope", None)),
        "scope_exceeds_evidence": getattr(angle, "scope_exceeds_evidence", None),
        "scope_declared_missing": getattr(angle, "scope_declared_missing", None),
        "has_assessment": assessment is not None,
    }
    if assessment is not None:
        detail["assessment"] = {
            "evidence_support": assessment.evidence_support,
            "evidence_support_raw": assessment.evidence_support_raw,
            "source_diversity": assessment.source_diversity,
            "counterpoint_awareness": assessment.counterpoint_awareness,
            "unknown_pressure": assessment.unknown_pressure,
            "specificity": assessment.specificity,
            "novelty": assessment.novelty,
            "newness_contribution": assessment.newness_contribution,
            "confidence_raw": assessment.confidence_raw,
            "viewpoint_score_raw": assessment.viewpoint_score_raw,
            "bound_facts": assessment.bound_facts,
            "total_facts": assessment.total_facts,
            "bound_sources": assessment.bound_sources,
            "bound_authors": assessment.bound_authors,
            "missing_author_sources": assessment.missing_author_sources,
            "declared_counterpoints": assessment.declared_counterpoints,
            "bound_counterpoints": assessment.bound_counterpoints,
            "total_materials": assessment.total_materials,
            "unreferenced_materials": assessment.unreferenced_materials,
            "breadth_markers": list(assessment.breadth_markers),
            "originality_claim": assessment.originality_claim,
            "restates_fact": assessment.restates_fact,
            "duplicate_group": assessment.duplicate_group,
        }
    return detail


def pack_of(state) -> object:
    """这一轮的证据包，**保证是模型而不是 dict**。

    `evidence_index` / `pack_detail` 一律用 `getattr` 读包（鸭子类型）。
    喂一个 JSON 解出来的 dict 进去，`getattr(dict, "facts")` 会**安静地**返回
    None：整包被判成空的，所有分数算成 0，报告于是写出一句"不可复现"——
    算得出数，但算的是另一件事，而且看起来像一条真结论。生产路径上
    `ainvoke` 会把它还原成 `EvidencePack`；这里把剩下的那条路堵掉，
    免得下次有人从 JSON 现造一份 state 时被骗。

    （`scripts/make_viewpoint_fixtures.py::_score` 踩过同一个坑，那里也钉了一句。）
    """
    from collections.abc import Mapping

    from app.agent.schemas import EvidencePack

    pack = state.get("evidence_pack")
    if isinstance(pack, Mapping):
        return EvidencePack.model_validate(pack)
    return pack


def pack_detail(pack) -> dict:
    return {
        "facts": len(getattr(pack, "facts", []) or []),
        "sources": len(getattr(pack, "source_ids", []) or []),
        "consensus": len(getattr(pack, "consensus", []) or []),
        "counterpoints": len(getattr(pack, "counterpoints", []) or []),
        "unknowns": len(getattr(pack, "unknowns", []) or []),
    }


# ---------------------------------------------------------------------------
# 真跑一次 Agent
# ---------------------------------------------------------------------------


async def run_agent(runtime, session_factory, goal: str, label: str) -> dict:
    """真跑一次（真实 X 检索 + 真实 LLM），返回这一轮的关键产物。"""
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=goal)
        run_id = str(run.id)

    state = await runtime.graph.ainvoke(
        {"run_id": run_id, "goal": goal}, config={"configurable": {"thread_id": run_id}}
    )
    async with session_factory() as session:
        row = (
            await session.execute(select(AgentRun).where(AgentRun.id == run.id))
        ).scalars().first()

    angles = list(state.get("angles") or [])
    selected = state.get("selected_angle")
    actions = [
        getattr(entry, "action", "") for entry in (state.get("decision_trace") or [])
    ]
    return {
        "label": label,
        "goal": goal,
        "run_id": run_id,
        # 走到人工审核的判据是**图在 human_review 处中断**，不是数据库里那一列。
        # `agent_run.status` 的 "waiting_for_review" 由 API 层在收到
        # `__interrupt__` 之后才写（app/main.py:213-216），而这里像 smoke 5B.1/5B.2
        # 一样直接调 `graph.ainvoke`——绕过 API 层就没有人去写那一列，它留在
        # "running"。拿它当判据会得到一句与事实相反的结论：明明停在人工审核，
        # 报告却说"没到"。
        "interrupted": "__interrupt__" in state,
        "db_status": getattr(row, "status", None),
        "state": state,
        "pack": pack_detail(state.get("evidence_pack")),
        "topic": state.get("selected_topic") or "",
        "angles": [angle_detail(a) for a in angles],
        "selected_angle_id": getattr(selected, "angle_id", None),
        "selected_claim": getattr(selected, "claim", ""),
        "actions": [a for a in actions if a],
        "steps": state.get("step_count"),
        # `finish_reason` **不是** state 上的键：它由 `metrics.finish_reason`
        # 从 decision_trace 里最后一条 finish 反推出来。直接读 state 恒得 None，
        # 而报告里那个 null 会被读成"这一轮没有收尾理由"——一个不存在的结论。
        # 这里调生产函数，报告与控制台显示的于是是同一个字符串。
        "finish_reason": metrics.finish_reason(state),
        "has_draft": bool(state.get("draft_id")),
        "guardrail_passed": state.get("guardrail_passed"),
    }


def _index_of(run: dict):
    """这一次运行的真实证据索引——确定性对照用的就是它。"""
    state = run["state"]
    return viewpoint.evidence_index(
        pack_of(state),
        state.get("research_item_ids") or [],
        state.get("research_results") or [],
    )


def _score_one(run: dict, draft: AngleDraft) -> viewpoint.ViewpointScore:
    index = _index_of(run)
    return viewpoint.score_viewpoints([draft], index, total_facts=len(index.fact_ids))[0]


# ---------------------------------------------------------------------------
# Smoke 1：正常热点的评估明细
# ---------------------------------------------------------------------------


def smoke_1_normal(run: dict) -> dict:
    """Smoke 1：热点目标下，Agent 形成多个候选观点并确定性地选了一个。"""
    order_by_id = [a["angle_id"] for a in run["angles"]]
    order_by_score = [
        a["angle_id"] for a in sorted(run["angles"], key=lambda a: -(a["viewpoint_score"] or 0.0))
    ]
    replay = score_replay(run["state"])
    return {
        "source_run": run["run_id"],
        "goal": run["goal"],
        "db_status": run["db_status"],
        "topic": run["topic"],
        "steps": run["steps"],
        "finish_reason": run["finish_reason"],
        "evidence": run["pack"],
        "candidate_count": len(run["angles"]),
        "angles": run["angles"],
        "selected_angle_id": run["selected_angle_id"],
        "selected_claim": run["selected_claim"],
        # 编号是在**确定性排序之后**赋的，所以"按 id 排"必须等于"按分数排"。
        # 不相等说明有一处的排序口径变了——那时界面上的 a1 就不是最高分。
        "id_order_matches_score_order": order_by_id == order_by_score,
        "id_order": order_by_id,
        "score_order": order_by_score,
        "score_replay": replay,
    }


# ---------------------------------------------------------------------------
# Smoke 2：证据冲突
# ---------------------------------------------------------------------------


def smoke_2_conflict(run: dict) -> dict:
    """Smoke 2：证据里有反方时，观点要绑得住它，立场要随之收敛。"""
    pack_counterpoints = run["pack"]["counterpoints"]
    observed = [
        {
            "angle_id": a["angle_id"],
            "claim": a["claim"],
            "counterpoints": len(a["counterpoints"]),
            "bound_counterpoint_evidence": a["counterpoint_evidence_ids"],
            "declared_counterpoints": (a.get("assessment") or {}).get(
                "declared_counterpoints"
            ),
            "bound_counterpoints": (a.get("assessment") or {}).get("bound_counterpoints"),
            "counterpoint_awareness": (a.get("assessment") or {}).get(
                "counterpoint_awareness"
            ),
            "stance": a["stance"],
            "max_stance": a["max_stance"],
        }
        for a in run["angles"]
    ]
    any_bound = any((a.get("assessment") or {}).get("bound_counterpoints") for a in run["angles"])
    any_conservative = any(
        a["stance"] in ("cautious", "uncertain", "moderate") for a in run["angles"]
    )

    # 确定性对照：拿**这一次真实的**证据索引，喂一个"声称毫无争议"的观点进去。
    # 真实检索当天有没有反方，脚本管不着；但"有反方却当它不存在"会不会被压住，
    # 是策略问题，必须在真实输入上成立。
    reference = _reference_draft(run)
    denial = _score_one(
        run,
        reference.model_copy(
            update={
                "counterpoints": [],
                "counterpoint_evidence_ids": [],
                "stance": ViewpointStance.STRONG,
            }
        ),
    )
    return {
        "source_run": run["run_id"],
        "goal": run["goal"],
        "evidence_counterpoints": pack_counterpoints,
        "evidence_has_counterpoints": pack_counterpoints > 0,
        "observed_angles": observed,
        "any_bound_counterpoint": bool(any_bound),
        "any_conservative_stance": bool(any_conservative),
        # **这一条是这次真机到底有没有测到那个性质的回答**。为 false 时上面
        # 那些 true/false 都不算数——不能把"当天没有反方"读成"处理反方的能力正常"。
        "property_exercised": pack_counterpoints > 0,
        "counterweight": {
            "note": (
                "把这一轮真实的证据索引喂给生产的打分函数，输入是一个自称毫无争议、"
                "并申报 strong 立场的观点。它检验的是策略，不是当天的巧合。"
            ),
            "evidence_counterpoints_in_index": len(_index_of(run).counterpoints),
            "counterpoint_awareness": denial.counterpoint_awareness,
            "stance_declared": _plain(denial.stance_declared),
            "stance": _plain(denial.stance),
            "max_stance": _plain(denial.max_stance),
            "downgraded": denial.stance_declared != denial.stance,
            "confidence": denial.confidence,
            "confidence_raw": denial.confidence_raw,
        },
    }


# ---------------------------------------------------------------------------
# Smoke 3：证据不足
# ---------------------------------------------------------------------------


def smoke_3_thin(run: dict) -> dict:
    """Smoke 3：证据不足时不许强行给强观点。"""
    # 真实观察一：所有存下来的观点，生效立场都不得越过证据推出的上界。
    breaches = [
        {
            "angle_id": a["angle_id"],
            "stance": a["stance"],
            "max_stance": a["max_stance"],
        }
        for a in run["angles"]
        # 用公开的 `STANCE_ORDER` 取下标的，不是模块私有的那张表：
        # 这个顺序是策略的一部分，脚本引用一份副本就等于多了一个会漂的口径。
        if a["stance"]
        and a["max_stance"]
        and viewpoint.STANCE_ORDER.index(ViewpointStance(a["stance"]))
        > viewpoint.STANCE_ORDER.index(ViewpointStance(a["max_stance"]))
    ]

    reference = _reference_draft(run)

    # 确定性对照一：一条事实都绑不上。空证据的强判断必须落回 uncertain。
    empty = _score_one(run, reference.model_copy(update={"evidence_ids": []}))
    # 确定性对照二：只绑得到**一个来源**的事实。
    single_ids = _single_source_fact_ids(run)
    single = _score_one(run, reference.model_copy(update={"evidence_ids": single_ids}))
    # 对照组的参照值：同一句话、按它**本来**绑定的那批证据算。
    # 没有这一项，"单一来源 0.23"这个数就没有可比的对象，读不出它是高还是低。
    full = _score_one(run, reference)

    return {
        "source_run": run["run_id"],
        "goal": run["goal"],
        "evidence": run["pack"],
        "candidate_count": len(run["angles"]),
        "stances": [{"angle_id": a["angle_id"], "stance": a["stance"]} for a in run["angles"]],
        "stance_exceeds_ceiling": breaches,
        "counterweights": {
            "no_evidence": {
                "note": "绑不上任何事实的观点，上界推不到 strong。",
                "evidence_support": empty.evidence_support,
                "bound_facts": empty.bound_facts,
                "max_stance": _plain(empty.max_stance),
                "stance": _plain(empty.stance),
                "viewpoint_score": empty.viewpoint_score,
                "low_support_cap": viewpoint.LOW_SUPPORT_VIEWPOINT_CAP,
                "score_below_low_support_cap": empty.viewpoint_score
                <= viewpoint.LOW_SUPPORT_VIEWPOINT_CAP,
            },
            "single_source": {
                "note": "只绑到一个来源的事实，证据支撑度会被单一来源上限压住。",
                "evidence_ids": single_ids,
                "available": bool(single_ids),
                "evidence_support": single.evidence_support,
                "evidence_support_raw": single.evidence_support_raw,
                "single_source_cap": viewpoint.SINGLE_SOURCE_EVIDENCE_CAP,
                "cap_applied": (
                    single.evidence_support_raw > single.evidence_support
                    if single_ids
                    else False
                ),
                "bound_sources": single.bound_sources,
                "max_stance": _plain(single.max_stance),
                "viewpoint_score": single.viewpoint_score,
                # 参照：同一句话按它本来绑定的那批证据算。两者一比，
                # "证据少一半"在分数上是什么样子才看得见。
                "reference_bound_sources": full.bound_sources,
                "reference_evidence_support": full.evidence_support,
                "reference_viewpoint_score": full.viewpoint_score,
            },
        },
    }


def _single_source_fact_ids(run: dict) -> list[str]:
    """挑一组"来源恰好只有一个"的事实 id，送给单一来源那条对照。

    挑不出来（每条事实都跨多个来源）是可能的，那时返回空列表并如实记为
    `available: false`——编一个出来会伪造一次并不存在的对照。
    """
    pack = pack_of(run["state"])
    groups: dict[frozenset[str], list[str]] = {}
    for fact in getattr(pack, "facts", []) or []:
        sources = frozenset(getattr(fact, "source_ids", []) or [])
        if len(sources) == 1:
            groups.setdefault(sources, []).append(fact.fact_id)
    if not groups:
        return []
    # 取条数最多的那一组：绑得越多，"分数低是因为来源少"这句话才越没有别的解释。
    return max(groups.values(), key=len)


# ---------------------------------------------------------------------------
# Smoke 4：新颖性与具体性的区分度
# ---------------------------------------------------------------------------


def smoke_4_novelty(run: dict) -> dict:
    """Smoke 4：`novelty` / `specificity` 在候选之间分得开，且与证据支撑度分开。"""
    rows = [
        {
            "angle_id": a["angle_id"],
            "claim": a["claim"],
            "novelty": (a.get("assessment") or {}).get("novelty"),
            "specificity": (a.get("assessment") or {}).get("specificity"),
            "evidence_support": (a.get("assessment") or {}).get("evidence_support"),
            "newness_contribution": (a.get("assessment") or {}).get("newness_contribution"),
            "restates_fact": (a.get("assessment") or {}).get("restates_fact"),
            "duplicate_group": (a.get("assessment") or {}).get("duplicate_group"),
            "viewpoint_score": a["viewpoint_score"],
        }
        for a in run["angles"]
    ]
    spread = _spread(rows)
    # novelty 在真实数据上**可能没有区分度**：它的确定性部分问的是"这条是不是
    # 复述已知材料 / 与同批另一条近似重复"，四条各说各话的观点在这两项上完全
    # 可以都判 1.0。那时"新颖性最高的是哪一条"是任意的，拿它去比"证据最强的是
    # 哪一条"，得到的 `False` 会被读成"这两项其实是一回事"——一个与事实相反的
    # 结论。没有分辨率就记 `null`，与 eval 里 `unavailable ≠ pass` 同一条纪律。
    varies = spread["novelty"] not in (None, 0.0)
    return {
        "source_run": run["run_id"],
        "goal": run["goal"],
        "candidate_count": len(rows),
        "rows": rows,
        "spread": spread,
        # §15：新颖性必须与证据支撑度**分开**。两者若永远同序，那 novelty
        # 就是 evidence_support 换了个名字——多出来的那一项没有在回答新问题。
        "top_by_novelty": _top_id(rows, "novelty") if varies else None,
        "top_by_evidence": _top_id(rows, "evidence_support"),
        "novelty_and_evidence_order_differ": (
            _top_id(rows, "novelty") != _top_id(rows, "evidence_support")
            if varies
            else None
        ),
        "novelty_comparison_note": (
            "本次 novelty 在候选之间完全相同（极差 0），这一项没有分辨率；"
            "真正在变的通常是 newness_contribution —— novelty 乘上证据门控之后的值。"
            if not varies
            else "本次 novelty 在候选之间有区分度，可以比较它与证据支撑度的排序。"
        ),
    }


def _spread(rows: list[dict]) -> dict:
    """每个分项的极差。候选不足两条、或该项没算出来时给 null。

    给 null 而不是 0：极差 0 的意思是"这几条观点在这一项上一模一样"，
    与"只有一条候选，谈不上区分度"是两件事。
    """
    out: dict = {}
    for field_name in (
        "novelty",
        "specificity",
        "evidence_support",
        # `newness_contribution` 一并列出：novelty 常常是常数，而它与
        # 评分里真正用的那一项（乘过证据门控的那个）不是同一个数。
        "newness_contribution",
    ):
        values = [r[field_name] for r in rows if r.get(field_name) is not None]
        out[field_name] = (
            round(max(values) - min(values), 4) if len(rows) >= 2 and len(values) >= 2 else None
        )
    return out


def _top_id(rows: list[dict], field_name: str) -> str | None:
    usable = [r for r in rows if r.get(field_name) is not None]
    if not usable:
        return None
    return max(usable, key=lambda r: r[field_name])["angle_id"]


# ---------------------------------------------------------------------------
# Smoke 5：全链路
# ---------------------------------------------------------------------------


def smoke_5_chain(run: dict) -> dict:
    """Smoke 5：从观点一路到 `waiting_for_review`，每一跳都在。"""
    state = run["state"]
    selected = state.get("selected_angle")
    selected_id = getattr(selected, "angle_id", None)
    candidates = list(state.get("candidates") or [])
    critics = list(state.get("critic_results") or [])
    recipe = state.get("style_recipe")

    return {
        "source_run": run["run_id"],
        "goal": run["goal"],
        "db_status": run["db_status"],
        "db_status_note": (
            "直接调图、绕过 API 层，所以数据库那一列停在 running；"
            "进入人工审核的事实以 interrupted 为准（见 scripts/smoke_5b3.py 的注释）。"
        ),
        "steps": run["steps"],
        "finish_reason": run["finish_reason"],
        "guardrail_passed": run["guardrail_passed"],
        "actions": run["actions"],
        "reached_human_review": run["interrupted"],
        # 每一项都是"这一跳在不在"。护栏的**结论**另开一个键：把它放进这张
        # 表里，一次护栏判不通过的运行会让 `links` 里出现一个 false，
        # 而那张表读起来是"少了哪一环"——两个不同的问题挤在同一处。
        "links": {
            "evidence_pack": run["pack"]["facts"] > 0,
            "selected_angle": selected is not None,
            "style_recipe": recipe is not None,
            "draft": run["has_draft"],
            "guardrail_evaluated": run["guardrail_passed"] is not None,
        },
        "selected_angle_id": selected_id,
        # §26：正文必须回指它该表达的那个观点，且**不采信模型的声明**。
        # 有一条对不上，就说明"Writer 不得替换 thesis"这条没有可检查的抓手。
        "candidate_viewpoint_refs": [
            {
                "candidate_id": getattr(c, "candidate_id", ""),
                "viewpoint_ref": getattr(c, "viewpoint_ref", ""),
            }
            for c in candidates
        ],
        "all_candidates_reference_selected": bool(candidates)
        and all(getattr(c, "viewpoint_ref", "") == selected_id for c in candidates),
        "critic_viewpoint_fit_present": [
            {
                "candidate_id": getattr(c, "candidate_id", ""),
                "has_viewpoint_fit": getattr(c, "viewpoint_fit", None) is not None,
                "overclaim_risk": getattr(
                    getattr(c, "viewpoint_fit", None), "overclaim_risk", None
                ),
                "viewpoint_consistency": getattr(
                    getattr(c, "viewpoint_fit", None), "viewpoint_consistency", None
                ),
            }
            for c in critics
        ],
        # `{{VIEWPOINT}}` 没被替换是**静默失败**（render 只替换传进去的键），
        # 所以这里直接问渲染函数要一段文本，确认它从不落到字面量上。
        "viewpoint_render_has_placeholder": "{{" in format_viewpoint(selected),
        "scope_signals_text": format_scope_signals(selected)[:200],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _reference_draft(run: dict) -> AngleDraft:
    """确定性对照的底稿：抄这一轮真实选中的那个观点，只改被对照的那一项。

    用真实的 claim 而不是一句编的：`specificity` 与全称量词检测都读 claim
    原文，换成一句假话，量到的就不是这一轮的东西了。
    """
    state = run["state"]
    selected = state.get("selected_angle")
    if selected is None:
        raise RuntimeError("这一轮没有选定观点，无法构造对照输入")
    return AngleDraft(
        claim=selected.claim,
        reasoning=getattr(selected, "reasoning", ""),
        why_it_matters=getattr(selected, "why_it_matters", ""),
        evidence_ids=list(selected.evidence_ids),
        counterpoints=list(getattr(selected, "counterpoints", []) or []),
        counterpoint_evidence_ids=list(
            getattr(selected, "counterpoint_evidence_ids", []) or []
        ),
        claim_scope=getattr(selected, "claim_scope", None),
        stance=getattr(selected, "stance_declared", None),
    )


async def _attempt_run(runtime, session_factory, key: str) -> tuple[dict | None, str]:
    """跑一轮，把异常变成一条记录而不是一次中断。

    返回 `(run, "")` 或 `(None, 错误说明)`。真机上一次运行的失败原因很多
    （X 400、LLM 解析失败、检索不到可成聚类的材料），其中没有一条值得
    连带毁掉另外两条已经跑完的记录。
    """
    try:
        return await run_agent(runtime, session_factory, GOALS[key], key), ""
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        return None, f"{type(exc).__name__}: {exc}"


async def run_() -> int:
    from app.config import settings

    url = settings.langgraph_checkpoint_database_url or settings.database_url
    runs: dict[str, dict] = {}
    failures: dict[str, str] = {}
    report: dict = {
        "phase": "5B.3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "5 个真机验收观察面的记录（工作单第三十三、三十四节）。内容是当天真实 X "
            "检索与真实 LLM 的产物，因此**不可复现**——它不是基准，是当时的记录。"
            "工作单第四十二节：真机不要求每次观点相同。"
        ),
        "goals": GOALS,
        "smokes": {},
    }

    async with create_checkpointer(url) as checkpointer:
        runtime = build_runtime(checkpointer)
        session_factory = runtime.session_factory

        # --- 三次真实运行 ---
        for key, label in (
            ("hotspot", "A（热点）"),
            ("conflict", "B（争议）"),
            ("thin", "C（冷门）"),
        ):
            print(f">>> 运行 {label}：{GOALS[key][:24]}…", flush=True)
            run, error = await _attempt_run(runtime, session_factory, key)
            if run is None:
                failures[key] = error
                print(f"    **这一轮失败了**：{error}", flush=True)
            else:
                runs[key] = run
                _print_run(run)

    report["runs"] = [
        {k: v for k, v in run.items() if k != "state"} for run in runs.values()
    ]
    report["failed_runs"] = failures

    # 一条运行失败**不丢掉其余两条**：真机上"当天检索回来的帖子不成聚类"
    # 是会发生的事，让它把另外两份记录一起带走，只会逼人重跑一遍。
    # 但依赖它的观察面一律记成 `available: false` 并写明原因——
    # 跳过与通过必须长得不一样，否则这份报告会开始骗人。
    for name, source, check in (
        ("1_normal_hotspot", "hotspot", smoke_1_normal),
        ("2_conflicting_evidence", "conflict", smoke_2_conflict),
        ("3_insufficient_evidence", "thin", smoke_3_thin),
        ("4_novelty_specificity", "hotspot", smoke_4_novelty),
        ("5_full_chain", "hotspot", smoke_5_chain),
    ):
        if source not in runs:
            report["smokes"][name] = {
                "available": False,
                "reason": f"来源运行失败，这一面没有数据：{failures.get(source)}",
            }
            continue
        try:
            report["smokes"][name] = check(runs[source])
        except Exception as exc:  # noqa: BLE001 - 报告优先，异常要有去处
            report["smokes"][name] = {
                "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }

    report["x_requests"] = {
        "total": len(X_REQUESTS),
        "by_method": _method_counts(),
        "by_endpoint": _endpoint_counts(),
        "queries": sorted({r["query"] for r in X_REQUESTS if "query" in r}),
    }
    _print_summary(report)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    document: dict = {}
    if REPORT_PATH.exists():
        try:
            loaded = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
        except json.JSONDecodeError:
            document = {}
    document.setdefault("phase", "5B.3")
    document.setdefault("note", "每次 smoke 一份记录，累积保存在 runs 里。")
    document.setdefault("runs", []).append(report)
    REPORT_PATH.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\n记录已写入 {REPORT_PATH}")
    # 报告已经落盘才谈退出码：一份缺了几面的记录比一次中断有用得多。
    # 但只要有一条运行真的失败了，这一次 smoke 就**没有被完成**，
    # 不能以一个成功的退出码收场。
    if failures:
        print(f"注意：{len(failures)} 轮运行失败 —— {json.dumps(failures, ensure_ascii=False)}")
        return 1
    return 0


def _print_run(run: dict) -> None:
    print(
        f"    run={run['run_id']} db_status={run['db_status']} "
        f"interrupted={run['interrupted']} "
        f"证据 {run['pack']['facts']} 条事实 / {run['pack']['sources']} 个来源 / "
        f"{run['pack']['counterpoints']} 条反方，"
        f"候选观点 {len(run['angles'])} 个，选中 {run['selected_angle_id']}",
        flush=True,
    )


def _method_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in X_REQUESTS:
        counts[entry["method"]] = counts.get(entry["method"], 0) + 1
    return counts


def _endpoint_counts() -> dict[str, int]:
    """按 `方法 路径` 计数。工作单第三十四节点名要求的两项就在这里。"""
    counts: dict[str, int] = {}
    for entry in X_REQUESTS:
        key = f"{entry['method']} {entry['path']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _print_summary(report: dict) -> None:
    print("\n" + "=" * 68)
    for name, payload in report["smokes"].items():
        print(f"\n{name}")
        if not payload.get("available", True):
            # 与通过长得**不一样**：这一面没有数据。没有这一支，一份缺了
            # 三条观察面的报告在屏幕上会与齐全的那份一模一样。
            print(f"  未取得数据：{payload.get('reason')}")
        elif name == "1_normal_hotspot":
            print(
                f"  候选 {payload['candidate_count']} 个，选中 {payload['selected_angle_id']}，"
                f"编号序 == 分数序：{payload['id_order_matches_score_order']}"
            )
            replay = payload["score_replay"]
            print(
                f"  分数复算：可比 {replay.get('angles_compared')} 个 × "
                f"{replay.get('fields_per_angle')} 项，"
                f"不一致 {len(replay.get('mismatches') or [])} 项"
            )
        elif name == "2_conflicting_evidence":
            print(
                f"  证据里的反方 {payload['evidence_counterpoints']} 条，"
                f"性质是否被检验到：{payload['property_exercised']}；"
                f"绑定反方的观点：{payload['any_bound_counterpoint']}"
            )
            print(f"  对照：{json.dumps(payload['counterweight'], ensure_ascii=False)}")
        elif name == "3_insufficient_evidence":
            print(f"  立场越界：{len(payload['stance_exceeds_ceiling'])} 个")
            print(f"  对照：{json.dumps(payload['counterweights'], ensure_ascii=False)}")
        elif name == "4_novelty_specificity":
            print(f"  区分度 {json.dumps(payload['spread'], ensure_ascii=False)}")
            print(
                f"  新颖性最高与证据最强不是同一个："
                f"{payload['novelty_and_evidence_order_differ']}"
                f"（{payload.get('novelty_comparison_note', '')}）"
            )
        elif name == "5_full_chain":
            print(f"  链路 {json.dumps(payload['links'], ensure_ascii=False)}")
            print(
                f"  到达人工审核：{payload['reached_human_review']}；"
                f"候选全部回指选定观点：{payload['all_candidates_reference_selected']}"
            )
    requests = report["x_requests"]
    print("\n请求：", json.dumps(requests["by_method"], ensure_ascii=False))
    print("按端点：", json.dumps(requests["by_endpoint"], ensure_ascii=False))
    counts = requests["by_endpoint"]
    tweets = counts.get("POST /2/tweets", 0)
    media = counts.get("POST /2/media", 0)
    print(f"POST /2/tweets = {tweets}（必须为 0）")
    print(f"POST /2/media  = {media}（必须为 0）")


def main() -> int:
    install_read_only_guard()
    return asyncio.run(run_())


if __name__ == "__main__":
    sys.exit(main())
