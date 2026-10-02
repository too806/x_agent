"""生成 Phase 5B.3 的观点类 fixture（`evals/fixtures/viewpoint_*.json`）。

跑法：

    python scripts/make_viewpoint_fixtures.py

## 为什么要有这个脚本

RULE-023 要求"观点分能从原始输入复算出来"。这条要求有一个副作用，写在
`rules.py` 的注释里，也是 RULE-017 当年踩过的：**fixture 里那些分数是"当时那个
口径下的分数"**，`viewpoint.py` 的权重或口径一变，它们就该红。

红是对的。但如果那些数字是手敲进去的，接住这次红的人只能靠肉眼把一个观点上的
三十来个数逐个重算——那不会发生，实际发生的会是把 fixture 的期望值改成"看起来
差不多"然后再改期望。所以这些数字**由生产代码算**，脚本留在这里，让"口径变了"
这件事的处置方式是**跑一遍这个脚本、看清 diff、确认它是预期的口径变化**。

## 它生成什么

五份 fixture 共用同一份 5B.3 状态（证据包 + 材料 + 三个观点），只在**一个变量**
上分叉——这样任何一条规则红了，能确定是那一个变量造成的：

    viewpoint_supported            干净：四条新门禁全过
    viewpoint_no_reasoning         少了推理层与自我反驳（RULE-020）
    viewpoint_overclaim            申报范围超出证据（RULE-021）
    viewpoint_counterpoint_erased  证据里有反方，观点与正文都当它不存在（RULE-022）
    viewpoint_score_drifted        分数被改动过 0.07（RULE-023）

**每份都重新算过分数**：改了一个变量却不重算，会让 RULE-023 连带变红，
那样"这条规则在说什么"就没法从 gate_failures 里读出来了。

## 它不做什么

不碰网络、不碰 LLM、不读数据库。基线来自 `healthy_agent_run.json`
（Phase 5A 的轨迹原样保留），只替换观点那一层。轨迹里那些内联计数器
（`facts=3`、`materials=5`）描述的是当年那次 5A 运行，本脚本不去改它们：
`evals/selftest.py` 按 `RULE-011` 的口径只比对步数/研究次数/改稿次数三个数，
那三个没有动。这一条写在 `provenance` 里。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.schemas import AngleDraft, EvidencePack  # noqa: E402
from app.agent.tools.angles import _assessment_of  # noqa: E402
from app.agent.viewpoint import evidence_index, rank_key, score_viewpoints  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures"
BASE = FIXTURES / "healthy_agent_run.json"

# --- 材料 -------------------------------------------------------------------
#
# 七条材料、七位作者。作者名刻意两两不同：来源多样性按**作者**去重，
# 而"同一个人的七条材料"正是这条规则要区分的反面。
MATERIALS: tuple[tuple[str, str, str], ...] = (
    ("ri-1", "alice", "第三方评测把同一批任务跑了两遍，两遍的成败判定并不一致。"),
    ("ri-2", "bob", "另一份评测沿用了自己的一套判定口径，与上一份无法直接比较。"),
    ("ri-3", "carol", "其中一份评测的样本量只有 40 条，结论里没有写置信区间。"),
    ("ri-4", "dave", "另一份没有公布样本量，只说结论来自「一批真实任务」。"),
    ("ri-5", "erin", "同一套 Agent 在两次工具升级之间成功率上升了 11 个百分点。"),
    ("ri-6", "frank", "失败案例里六成集中在工具返回异常之后的恢复分支。"),
    ("ri-7", "gina", "公开材料里没有披露「完成」的判定口径，也没有披露抽样方式。"),
)

#: 材料 id → (作者, 正文)。证据包的 `source_ids` 要覆盖全部七条，
#: 否则"没被任何事实引用的材料"会把未知压力顶上去（那是**另一条**路径，
#: 在 `evals/viewpoint/cases.py` 里单独验，不混进干净数据）。

#: 追加到基线证据包上的两条新事实。基线原有的三条（f1/f2/f3）**原样保留**：
#: 候选正文正是照着它们写的，改写它们会让"正文与证据对得上"这条性质失效。
NEW_FACTS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("f4", ("ri-1", "ri-2"), "两份第三方评测对同一批任务的成败判定互相矛盾。"),
    ("f5", ("ri-3", "ri-4"), "两份评测都没有披露样本量与判定口径。"),
)

CONSENSUS: tuple[str, ...] = ("单一一次的成功率不能代表 Agent 的可靠性",)
COUNTERPOINTS: tuple[str, ...] = (
    "有观点认为判定不一致只是标注差异，扩大样本量就能收敛",
)
UNKNOWNS: tuple[str, ...] = (
    "两家评测的分歧到底有多大",
    "判定口径统一之后结论会不会反过来",
)

# --- 三个观点 ---------------------------------------------------------------
#
# 逐条声明成草稿（`AngleDraft` 的同名形状），分数由 `viewpoint.score_viewpoints`
# 算出来之后回填。这保证 fixture 里那份"存下来的分"与打分器当下算的一致——
# 手敲的话，RULE-023 从第一天起就是红的。

DRAFTS: tuple[dict[str, object], ...] = (
    {
        "claim": "把「成功率」当成 Agent 可靠性的主指标，现在会误导采购决策",
        "reasoning": (
            "两份第三方评测对同一批任务的判定互相矛盾，而两份都没有披露样本量与判定口径；"
            "再加上失败案例高度集中在工具异常后的恢复分支，说明一个聚合出来的成功率"
            "既不可复现、也不指向具体的能力缺口。"
        ),
        "why_it_matters": "采购方按一个不可复现的数做决策，等于按噪声付钱",
        # 四条事实 / 五条来源 / 五位作者 → `evidence_scope` 落在 industry
        # （society 那一档要六条来源，见 `EVIDENCE_SCOPE_LADDER`）。
        # **刻意不绑满六条来源**：绑满会把证据范围推到 society，而 society 之上
        # 没有更高的范围可申报，`viewpoint_overclaim` 那份 fixture 就演示不出
        # "申报超出证据"了。范围阶梯本就是这个形状——证据越宽，能主张的范围越大。
        "evidence_ids": ["f1", "f2", "f3", "f5"],
        "risk": "如果两份评测的分歧只是标注差异，这条判断就把口径问题说成了指标问题",
        "counterpoints": ["也可能只是评测早期的常态，样本量上去就一致了"],
        "counterpoint_evidence_ids": ["f5"],
        "assumptions": ["被评测的 Agent 版本在两次评测之间没有变化"],
        "implications": ["采购方会开始要求评测方披露样本量与判定口径"],
        # 申报到**证据允许的上界**：这条判断知道自己的边界在哪。
        "claim_scope": "industry",
        "novelty_note": "把两份互相矛盾的评测与失败分布连起来看",
    },
    {
        "claim": "评测机构之间的口径分歧，本身就是这个领域还没成熟的信号",
        "reasoning": "两份评测对同一批任务给出不同判定，说明连「什么算完成」都还没有共识。",
        "why_it_matters": "口径不统一时，任何跨机构的横向比较都不成立",
        "evidence_ids": ["f4", "f5"],
        "risk": "把标注差异读成领域不成熟，跨度有点大",
        "counterpoints": [],
        "counterpoint_evidence_ids": [],
        "assumptions": [],
        "implications": [],
        "claim_scope": "case",
        "novelty_note": "从分歧本身而不是从某一方的结论出发",
    },
    {
        "claim": "披露样本量会变成评测机构的竞争点",
        "reasoning": "两份评测都没写样本量，而样本量恰好是判断结论强度的唯一入口。",
        "why_it_matters": "谁先披露，谁就先获得可比较的资格",
        "evidence_ids": ["f5"],  # 只有一条事实 → 支撑度低、立场只能是 uncertain
        "risk": "披露与否可能只是各家文档习惯的差别",
        "counterpoints": [],
        "counterpoint_evidence_ids": [],
        "assumptions": [],
        "implications": [],
        "claim_scope": "case",
        "novelty_note": "",
    },
)

#: 该被选中的观点。a1 的分最高（四条事实、六条来源、有绑定反方），
#: 与第二名的差距远大于 `viewpoint.TIE_MARGIN`，所以选定那一步不会是并列。
SELECTED = 0

#: 加在 `viewpoint_counterpoint_erased` 第一条正文前面的一句话。它走的是
#: RULE-022 的**第二条**路径（"正文声称无争议"），与"观点没绑定任何反方"
#: 那条路径并列——两条都写进去，是为了让规则在两条路径上的行为都被这份
#: fixture 覆盖到，而不是只覆盖读得分的那一条。
DENIAL_PROSE = "这件事其实没有争议，两家评测只是发布时间不同。"


def _draft(overrides: dict[str, object]) -> AngleDraft:
    payload = dict(DRAFTS[SELECTED])
    payload.update(overrides)
    return AngleDraft(**payload)


def _build(base: dict) -> dict:
    """基线 + 5B.3 的观点那一层。"""
    state = base["state"]
    pack = dict(state["evidence_pack"])

    facts = list(pack["facts"])
    for fact_id, source_ids, text in NEW_FACTS:
        facts.append(
            {"fact_id": fact_id, "fact": text, "source_ids": list(source_ids), "timestamp": None}
        )
    pack["facts"] = facts
    # 覆盖全部七条材料：否则"没被引用"会把未知压力顶到 0.57，
    # 那样上界只能是 cautious，干净数据就演示不出 strong 那条路。
    pack["source_ids"] = [item_id for item_id, _, _ in MATERIALS]
    pack["consensus"] = list(CONSENSUS)
    pack["counterpoints"] = list(COUNTERPOINTS)
    pack["unknowns"] = list(UNKNOWNS)

    state["evidence_pack"] = pack
    state["research_item_ids"] = [item_id for item_id, _, _ in MATERIALS]
    state["viewpoint_inputs"] = [
        {"item_id": item_id, "author": author, "content": content}
        for item_id, author, content in MATERIALS
    ]
    return base


def _score(base: dict, drafts: list[AngleDraft]) -> list[dict]:
    """用**生产**的打分器算，并摊成 fixture 里的 `Angle` 形状。

    编号与顺序照 `tools/angles.py` 的做法：先排序、再取 `a1`、`a2`…——
    名次对不上分数的编号比没有编号更误导。
    """
    state = base["state"]
    # 打分器一律用 `getattr` 读 pack（鸭子类型），所以这里必须给一个**真的**
    # `EvidencePack`：直接喂 JSON 解出来的 dict，`getattr(dict, "facts")` 会
    # 安静地返回 None，整份证据包被判成空的，所有分数变成 0——算得出数，但
    # 算的是另一件事。这一步也顺带在生成时就撞上 schema 漂移。
    pack = EvidencePack.model_validate(state["evidence_pack"])
    item_ids = [entry["item_id"] for entry in state["viewpoint_inputs"]]
    results = [
        _Material(entry["author"], entry["content"]) for entry in state["viewpoint_inputs"]
    ]
    index = evidence_index(pack, item_ids, results)
    total_facts = len({fact.fact_id for fact in pack.facts})
    scores = score_viewpoints(drafts, index, total_facts=total_facts)

    order = sorted(range(len(drafts)), key=lambda i: rank_key(scores[i]))
    angles: list[dict] = []
    for position, index_in_batch in enumerate(order, start=1):
        draft = drafts[index_in_batch]
        score = scores[index_in_batch]
        angle = {
            "angle_id": f"a{position}",
            "claim": draft.claim,
            "why_it_matters": draft.why_it_matters,
            "evidence_ids": list(draft.evidence_ids),
            "risk": draft.risk,
            "confidence": score.confidence,
            "reasoning": draft.reasoning,
            "counterpoints": list(draft.counterpoints),
            "counterpoint_evidence_ids": list(draft.counterpoint_evidence_ids),
            "assumptions": list(draft.assumptions),
            "implications": list(draft.implications),
            "novelty_note": draft.novelty_note,
            "restates_known_material": draft.restates_known_material,
            "stance": score.stance.value,
            "stance_declared": score.stance_declared.value if score.stance_declared else None,
            "max_stance": score.max_stance.value,
            "claim_scope": score.claim_scope.value if score.claim_scope else None,
            "evidence_scope": score.evidence_scope.value,
            "scope_exceeds_evidence": score.scope_exceeds_evidence,
            "scope_declared_missing": score.scope_declared_missing,
            "viewpoint_score": score.viewpoint_score,
            "assessment": _assessment_of(score).model_dump(mode="json"),
        }
        angles.append(angle)
    return angles


class _Material:
    """`evidence_index` 只读这两个字段（鸭子类型，同生产）。"""

    __slots__ = ("author_username", "content")

    def __init__(self, author: str, content: str) -> None:
        self.author_username = author
        self.content = content


def _write(label: str, base: dict, *, note: str) -> None:
    base["label"] = label
    base["note"] = note + NOTE_REGENERATE
    # `provenance` 是 Literal["fixture", "live-export", "recorded"]，不是自由文本：
    # 它是**报告里**用来防止合成数据被当成实测引用的那一个字段，不能拿它记事。
    # 生成方式写在 `note` 里。
    base["provenance"] = "fixture"
    base["synthetic"] = True
    path = FIXTURES / f"{label}.json"
    path.write_text(
        json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {path.relative_to(ROOT)}")


NOTE_REGENERATE = (
    "\n\n生成方式：scripts/make_viewpoint_fixtures.py 从 healthy_agent_run.json 派生，"
    "轨迹 / 预算 / 话题那几层原样保留，只替换证据包与观点层；分数与评估明细由 "
    "app/agent/viewpoint.py 实算后回填，不是手写的。改了口径就重跑那个脚本并核对 diff。"
    "trace 的 input_summary 里那些内联计数（facts=3、materials=5）描述的是当年那次 "
    "Phase 5A 运行，未做改动；RULE-011 只比对步数 / 研究次数 / 改稿次数，那三个数没有变。"
)


def _variant(selected: dict[str, object] | None = None) -> tuple[dict, list[dict]]:
    """基线 + 5B.3 那一层；`selected` 是对第一条（会被选中的那条）的改写。"""
    base = _build(json.loads(BASE.read_text(encoding="utf-8")))
    drafts = [AngleDraft(**payload) for payload in DRAFTS]
    if selected is not None:
        drafts[SELECTED] = _draft(selected)
    angles = _score(base, drafts)
    base["state"]["angles"] = angles
    # 排序后 a1 在最前（`_score` 已按 `rank_key` 排好），所以选定的是它。
    base["state"]["selected_angle"] = angles[0]
    return base, angles


def main() -> int:
    # --- 干净版 -------------------------------------------------------------
    clean, angles = _variant()
    _write(
        "viewpoint_supported",
        clean,
        note=(
            "synthetic fixture — Phase 5B.3 的正路：四条事实、六条来源、六位作者，"
            "反方绑到了事实，观点分由 Python 算出。四条新门禁（RULE-020~023）全部通过。"
            "与 viewpoint_overclaim / _no_reasoning / _counterpoint_erased / _score_drifted "
            "共用同一份状态，只在**一个变量**上分叉。"
        ),
    )

    # --- RULE-020：没有推理层与自我反驳 --------------------------------------
    thin, _ = _variant({"reasoning": "", "assumptions": [], "risk": ""})
    _write(
        "viewpoint_no_reasoning",
        thin,
        note=(
            "synthetic fixture — 与 viewpoint_supported 只差推理层：选定的观点"
            "reasoning 为空、assumptions 与 risk 也为空。分数一项都没变"
            "（这三项不进任何分数），所以只有 RULE-020 会红——这正是"
            "「事实到结论的那一步有没有被写出来」从分数上看不见的证据。"
        ),
    )

    # --- RULE-021：范围超出证据 ---------------------------------------------
    broad, _ = _variant({"claim_scope": "society"})
    _write(
        "viewpoint_overclaim",
        broad,
        note=(
            "synthetic fixture — 与 viewpoint_supported 只差**一句自我申报**：把 "
            "claim_scope 从 industry 抬到 society。绑定的证据、正文一个字、打分公式"
            "全都没动（证据撑到 industry）——RULE-021 必须 FAIL。它同时演示了"
            "工作单第二十九节的防操纵性质：申报只能触发标志，拿不到任何好处，"
            "观点分一分没涨，只换来一个更低的立场上界。"
        ),
    )

    # --- RULE-022：把已知的反方抹掉 ------------------------------------------
    erased, _ = _variant({"counterpoints": [], "counterpoint_evidence_ids": []})
    # 正文也当争议不存在：两条路径同时给出来，任一条都足以判失败。
    candidates = list(erased["state"]["candidates"])
    candidates[0] = dict(candidates[0], content=DENIAL_PROSE + candidates[0]["content"])
    erased["state"]["candidates"] = candidates
    _write(
        "viewpoint_counterpoint_erased",
        erased,
        note=(
            "synthetic fixture — 与 viewpoint_supported 只差反方：证据包的两条反方"
            "没动，观点既没声明也没绑定任何一条，第一条正文还加了一句"
            "「这件事其实没有争议」。RULE-022 必须 FAIL。它同时演示了这条规则的"
            "下限性质：抹掉反方让 confidence 掉了约 0.38，而那个差在报告里只是一行数字。"
        ),
    )

    # --- RULE-023：分数被改动 -------------------------------------------------
    drifted, drift_angles = _variant()
    drift_angles[0] = dict(
        drift_angles[0],
        viewpoint_score=round(drift_angles[0]["viewpoint_score"] + 7.0, 2),
    )
    drifted["state"]["angles"] = drift_angles
    drifted["state"]["selected_angle"] = drift_angles[0]
    _write(
        "viewpoint_score_drifted",
        drifted,
        note=(
            "synthetic fixture — 与 viewpoint_supported 只差一个数：把 a1 的"
            "viewpoint_score 抬高 7 分（其余字段全部保持复算得到的样子）。"
            "RULE-023 必须 FAIL，并且它演示的正是工作单第十二节要拦的那种数——"
            "一个看起来很像分数、却找不到任何一组输入能算出来的值。"
        ),
    )

    # 把干净版选定的那个观点打印出来：口径一变，这里的分与 stance 会跟着动，
    # 而"跟着动了多少"正是重新生成时要看的那一份 diff。
    top = angles[0]
    print(
        "\n干净版选定的观点：{claim}\n"
        "  viewpoint_score={score}  confidence={conf:.4f}  stance={stance}"
        "（上界 {cap}）\n"
        "  绑定 {facts} 条事实 / {sources} 条来源 / {authors} 位作者；"
        "反方 绑定 {cp} 条、声明 {declared} 条\n"
        "  evidence_scope={ev}  claim_scope={cs}  "
        "超出证据={over}".format(
            claim=top["claim"],
            score=top["viewpoint_score"],
            conf=top["confidence"],
            stance=top["stance"],
            cap=top["max_stance"],
            facts=top["assessment"]["bound_facts"],
            sources=top["assessment"]["bound_sources"],
            authors=top["assessment"]["bound_authors"],
            cp=top["assessment"]["bound_counterpoints"],
            declared=top["assessment"]["declared_counterpoints"],
            ev=top["evidence_scope"],
            cs=top["claim_scope"],
            over=top["scope_exceeds_evidence"],
        )
    )
    # 三个观点都写进 fixture，但 `selected_angle` 只指第一名。
    print(f"共 {len(angles)} 个观点写入 fixture，selected_angle 指向 a1")
    if top["stance"] != "strong":
        print(
            "警告：干净版没到 strong——RULE-020~023 仍然判得出来，但这份 fixture"
            "就不再演示『证据够时敢下判断』那条路了，先查证据包再改期望。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
