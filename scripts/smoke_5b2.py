"""Phase 5B.2 的真机验收 smoke（工作单第三十八节）。

五次 smoke，每次只观察与记录，判定规则一条都不加：

| Smoke | 做什么 | 看什么 |
|---|---|---|
| 1 | 同一 Evidence + Angle，**不接** recipe | 基线候选 |
| 2 | 同一 Evidence + Angle，**接** recipe | 与 1 比长度/结构/hook/信息密度 |
| 3 | 换一个 StyleRecipe | Writer 行为发生合理变化 |
| 4 | recipe 不存在 | Agent 仍能正常完成 |
| 5 | 同一 corpus 重复生成 Profile | 逐位相同 |

## 为什么 Smoke 1–3 不跑整条 Graph

工作单要求的是"**同一** Evidence + Angle"下换 recipe 看差异。整条 Graph 每次
都会重新检索、重新抽证据、重新选观点——**三个变量一起动**，跑出来的差异
根本归因不到 recipe 上。所以 Smoke 1–3 这样组织：

1. 先用**真实** X 检索 + 真实 LLM 跑一次完整的 Agent，把这一轮的
   EvidencePack 与 SelectedAngle **冻住**；
2. 再用**同一个** freeze 出来的输入，只换 `{{STYLE}}` 这一块，各写一轮。

于是三次之间唯一的差别就是 recipe。prompt 模板、persona、证据、观点全部相同。

## 比的是数，不是感觉

Smoke 2 与 Smoke 1 的对照全部用 `app/style/features.py` 的确定性函数算出来
（字数、句数、段数、每百字数字个数、具体实体数、问句数、开头/结尾形态）。
"读起来结构感更强"不是结论，`每百字数字个数 1.2 → 3.4` 才是。

## 绝不发布

与 `scripts/smoke_5b1.py` 同一套只读守卫：`RealXApiClient._request` 被替换，
非 GET **在发出去之前**就抛。报告里给出请求总数与按方法的分组，
`POST` 计数必须是 0。

运行：`python scripts/smoke_5b2.py`（需 PostgreSQL 在跑、.env 有 X 与 LLM 凭据）
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# 与 smoke_5b1 同一个理由：仓库根必须排在 sys.path 最前，否则 `import app`
# 可能静默拿到 editable 安装指向的另一个检出。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agent.persona import load_persona  # noqa: E402
from app.agent.prompts import (  # noqa: E402
    format_evidence,
    format_persona,
    format_style_recipe,
    load_prompt,
    render,
)
from app.agent.schemas import CandidateList  # noqa: E402
from app.agent.tools.base import complete_json  # noqa: E402
from app.db.checkpointer import create_checkpointer  # noqa: E402
from app.main import build_runtime  # noqa: E402
from app.models import StyleProfile, StyleSample  # noqa: E402
from app.repositories import create_agent_run  # noqa: E402
from app.services.llm import OpenAILLMService  # noqa: E402
from app.services.x_api import RealXApiClient  # noqa: E402
from app.style import features  # noqa: E402
from app.style.profile import compute_profile  # noqa: E402
from app.style.recipe import build_recipe, evidence_signals  # noqa: E402
from app.style.runtime import resolve_recipe  # noqa: E402
from app.style.schemas import (  # noqa: E402
    DensityLevel,
    EndingType,
    EvidenceRequirement,
    StanceStrength,
    StyleRecipe,
    Tone,
)
from app.style.taxonomy import HookType, StructureType  # noqa: E402
from evals.style.spans import (  # noqa: E402
    SHARED_SPAN,
    corpus_spans,
    overlapping_spans,
    spans_of,
)

REPORT_PATH = ROOT / "docs" / "phase-5b2-smoke-runs.json"

#: 跑一次真实 Agent 用的目标。与 Smoke 1–3 冻结的那批输入同源。
GOAL = "今天帮我找一个最近值得在中文科技 X 圈讨论的 AI Agent 话题。"

#: Smoke 3 用的第二个 recipe。与 Smoke 2 的**每一档都不同**，这样"换一个
#: recipe 行为会不会变"才真的被问到；只改一个字段的话，差异可能被别的
#: 约束盖住，然后被读成"recipe 没用"。
ALT_RECIPE = StyleRecipe(
    structure_type=StructureType.CONTRAST,
    hook_type=HookType.STATISTIC,
    observed_target_length=90,
    density=DensityLevel.HIGH,
    stance_strength=StanceStrength.STRONG,
    evidence_requirement=EvidenceRequirement.STRICT,
    tone=Tone.DIRECT,
    ending_type=EndingType.QUESTION,
    reasons=["（smoke 用）刻意与语料推导出的那一版在每一档上都相反"],
    profile_ref="smoke@alt",
)

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
            raise AssertionError(f"Phase 5B.2 smoke 只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 确定性代理指标
# ---------------------------------------------------------------------------


def measure(text: str) -> dict:
    """一条正文的确定性形态指标。**全部可复算**，不含任何模型判断。

    刻意与 `app/style/profile.py` 用同一套 `app/style/features.py` 原语：
    smoke 里量出来的数，与 Profile 里那个数，必须是同一个算法算的——
    否则"recipe 把稿子推向 Profile 描述的形态"这件事没法被检验。
    """
    sentences = features.split_sentences(text)
    paragraphs = features.split_paragraphs(text)
    return {
        "chars": len(text),
        "sentences": len(sentences),
        "paragraphs": len(paragraphs),
        "median_sentence_length": features.median(features.sentence_lengths(sentences)),
        # 信息密度的代理指标用 `number_density`（每百字数字个数，features 里
        # 定义好的口径），另外附一个**具体实体计数**。不自己再算一个
        # "每百字实体数"：那需要在这里复制一份 `_visible_length` 的分母约定，
        # 而两处分母一旦不一致，比较出来的差就有一半来自口径而不是来自 recipe。
        "numbers_per_100": _round(features.number_density(text)),
        "concrete_entities": len(features.find_concrete_entities(text)),
        "questions": features.count_questions(text),
        "emojis": features.emoji_count(text),
        "english_ratio": _round(features.english_ratio(text)),
        "opening_pattern": features.opening_pattern(text),
        "closing_pattern": features.closing_pattern(text),
        "generic_phrase_hits": len(features.find_generic_phrases(text)),
    }


def _round(value):
    return None if value is None else round(value, 3)


def aggregate(rows: list[dict]) -> dict:
    """一组候选的汇总。中位数而不是均值：三条候选里一条特别长就能把均值拽走。"""
    if not rows:
        return {}
    return {
        "chars_median": features.median([r["chars"] for r in rows]),
        "chars_min": min(r["chars"] for r in rows),
        "chars_max": max(r["chars"] for r in rows),
        "sentences_median": features.median([r["sentences"] for r in rows]),
        "paragraphs_median": features.median([r["paragraphs"] for r in rows]),
        "numbers_per_100_median": features.median(
            [r["numbers_per_100"] for r in rows if r["numbers_per_100"] is not None]
        ),
        "concrete_entities_median": features.median(
            [r["concrete_entities"] for r in rows]
        ),
        "questions_total": sum(r["questions"] for r in rows),
        "generic_phrase_hits_total": sum(r["generic_phrase_hits"] for r in rows),
        "openings": sorted({r["opening_pattern"] for r in rows}),
        "closings": sorted({r["closing_pattern"] for r in rows}),
    }


# ---------------------------------------------------------------------------
# 写一轮
# ---------------------------------------------------------------------------


def build_writer_prompt(frozen: dict, recipe: StyleRecipe | None) -> str:
    """渲染 Writer 的 prompt。**除了 STYLE 那一块，每个参数都来自冻结的输入。**"""
    angle = frozen["angle"]
    return render(
        load_prompt("candidates"),
        GOAL=GOAL,
        TOPIC=frozen["topic"],
        EVIDENCE=frozen["evidence_text"],
        CLAIM=getattr(angle, "claim", ""),
        WHY_IT_MATTERS=getattr(angle, "why_it_matters", ""),
        PERSONA=format_persona(load_persona()),
        STYLE=format_style_recipe(recipe),
        MAX_LENGTH=load_persona().max_length,
        COUNT=3,
    )


async def write_round(llm_service, frozen: dict, recipe: StyleRecipe | None) -> dict:
    """用冻结的输入写一轮，返回 prompt 与三条候选的确定性指标。"""
    prompt = build_writer_prompt(frozen, recipe)
    parsed: CandidateList = await complete_json(llm_service, prompt, CandidateList)
    rows = [measure(c.content) for c in parsed.candidates]
    return {
        "structure_type": _value(getattr(recipe, "structure_type", None)),
        "hook_type": _value(getattr(recipe, "hook_type", None)),
        "observed_target_length": getattr(recipe, "observed_target_length", None),
        "effective_target_length": getattr(recipe, "effective_target_length", None),
        "hard_max_length": getattr(recipe, "hard_max_length", None),
        "prompt_chars": len(prompt),
        # 未替换的占位符是**静默失败**：render 只替换传进去的键。
        "unreplaced_placeholders": prompt.count("{{"),
        "candidates": [{"content": c.content, **row} for c, row in zip(parsed.candidates, rows)],
        "metrics": aggregate(rows),
    }


def _value(enum) -> str | None:
    return None if enum is None else getattr(enum, "value", str(enum))


# ---------------------------------------------------------------------------
# 冻结输入：真跑一次 Agent
# ---------------------------------------------------------------------------


async def freeze_one_real_run(runtime, session_factory, llm_service) -> dict:
    """真跑一次完整 Agent，把 EvidencePack + SelectedAngle 冻下来。

    这是 Smoke 1–3 的共同输入。它**只有一次**：三次写稿之间唯一的差别必须
    是 recipe，所以证据与观点必须来自同一个 freeze，而不是各跑一遍。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=GOAL)
        run_id = str(run.id)

    state = await runtime.graph.ainvoke(
        {"run_id": run_id, "goal": GOAL}, config={"configurable": {"thread_id": run_id}}
    )
    pack = state.get("evidence_pack")
    angle = state.get("selected_angle")
    if pack is None or angle is None:
        raise RuntimeError(
            "这一轮没有产出证据包或选定观点，无法冻结输入；"
            f"actions={[getattr(a, 'action', a) for a in (state.get('decision_trace') or [])]}"
        )

    return {
        "run_id": run_id,
        "topic": state.get("selected_topic") or "",
        "angle": angle,
        # 证据包原件。Smoke 1/2 要用它取 recipe 的信号档位（与生产路径一致），
        # 但它**不进报告**——报告里只留 `evidence` 那几个计数。
        "pack": pack,
        "angle_id": getattr(angle, "angle_id", None),
        "claim": getattr(angle, "claim", ""),
        "evidence_text": format_evidence(pack),
        "evidence": {
            "facts": len(pack.facts),
            "sources": len(pack.source_ids),
            "counterpoints": len(pack.counterpoints),
            "unknowns": len(pack.unknowns),
        },
        "recipe_used": _recipe_summary(state.get("style_recipe")),
        "steps": state.get("step_count"),
    }


def _recipe_summary(recipe) -> dict | None:
    if recipe is None:
        return None
    return {
        "structure_type": _value(recipe.structure_type),
        "hook_type": _value(recipe.hook_type),
        "observed_target_length": recipe.observed_target_length,
        "effective_target_length": recipe.effective_target_length,
        "hard_max_length": recipe.hard_max_length,
        "density": _value(recipe.density),
        "ending_type": _value(recipe.ending_type),
        "profile_ref": recipe.profile_ref,
    }


# ---------------------------------------------------------------------------
# Smoke 5：确定性
# ---------------------------------------------------------------------------


async def corpus_reuse_check(session_factory, rounds: dict[str, dict]) -> dict:
    """§九：交付候选**不得搬运真实语料的原文**。

    与 `evals/style/selftest.py` 共用同一个检测器（`evals/style/spans.py`），
    只是换了一份语料：那边是 8 条**合成**样本，这里是库里**真实**采到的样本。
    两份语料走同一套判据，否则"离线基准通过"与"真实链路通过"会变成两个
    不同的命题，而它们本该是同一件事。

    ## 只记 post_id 与命中数，**绝不记片段本身**

    报告是要提交进仓库的。把命中的片段写进报告，就等于把别人账号的原文
    抄进了 Git——而这条检查存在的全部理由就是防止这件事。所以这里只回答
    "哪一条候选、撞上了哪个 post_id、撞了几个片段"，
    至于撞的是哪几个字，报告里一个字都没有。

    ## 反证

    同一段检测器再跑一次，输入是**故意掺进来的**一条真实样本原文。它必须
    报警。没有这一步，上面那个"0 个片段"可能只是检测器永远返回空集。
    """
    async with session_factory() as session:
        samples = (
            await session.execute(select(StyleSample).where(StyleSample.included.is_(True)))
        ).scalars().all()

    per_sample = {s.platform_post_id: spans_of(s.content) for s in samples}
    pooled = corpus_spans([s.content for s in samples])

    checked = 0
    offenders: list[dict] = []
    for round_name, payload in rounds.items():
        for index, candidate in enumerate(payload.get("candidates") or []):
            checked += 1
            shared = overlapping_spans(candidate["content"], pooled)
            if not shared:
                continue
            hits = sorted(
                post_id
                for post_id, spans in per_sample.items()
                if spans & set(shared)
            )
            offenders.append(
                {
                    "round": round_name,
                    "candidate_index": index,
                    "corpus_post_ids": hits,
                    "shared_spans": len(shared),
                    "shared_chars": len(shared) + SHARED_SPAN - 1,
                }
            )

    # 反证：掺一条真实样本原文进去，检测器必须红。
    poisoned = samples[0].content if samples else ""
    poisoned_hits = overlapping_spans(poisoned, pooled) if poisoned else []

    return {
        "corpus_samples": len(samples),
        "span_n": SHARED_SPAN,
        "candidates_checked": checked,
        "offenders": offenders,
        "passed": not offenders,
        "detector_self_test": {
            "poisoned_shared_spans": len(poisoned_hits),
            "fires": bool(poisoned_hits),
        },
    }


async def corpus_determinism(session_factory) -> dict:
    """同一份 corpus 连算两遍 Profile，逐字段比对。"""
    async with session_factory() as session:
        samples = (
            await session.execute(select(StyleSample).where(StyleSample.included.is_(True)))
        ).scalars().all()
        first = compute_profile(samples)
        second = compute_profile(samples)

    dump_a, dump_b = first.model_dump(), second.model_dump()
    differing = sorted(k for k in dump_a if dump_a[k] != dump_b[k])
    return {
        "sample_count": first.sample_count,
        "identical": dump_a == dump_b,
        "differing_fields": differing,
        "profile": dump_a,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def run() -> int:
    from app.config import settings

    llm_service = OpenAILLMService()
    url = settings.langgraph_checkpoint_database_url or settings.database_url
    report: dict = {
        "phase": "5B.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "5 次真机验收 smoke 的观察记录（工作单第三十八节）。内容是当天真实 X 检索"
            "与真实 LLM 的产物，因此**不可复现**——它不是基准，是当时的记录。"
        ),
        "goal": GOAL,
        "smokes": {},
    }

    runtime_session_factory = None
    async with create_checkpointer(url) as checkpointer:
        runtime = build_runtime(checkpointer)
        runtime_session_factory = runtime.session_factory

        print(">>> 冻结输入：真跑一次 Agent（真实 X + 真实 LLM）…", flush=True)
        frozen = await freeze_one_real_run(runtime, runtime.session_factory, llm_service)
        report["frozen_input"] = {
            k: v for k, v in frozen.items() if k not in ("angle", "pack")
        }
        print(
            f"    证据 {frozen['evidence']['facts']} 条事实 / "
            f"{frozen['evidence']['sources']} 个出处，观点 {frozen['angle_id']}",
            flush=True,
        )

        # --- Smoke 1 / 2 / 3：同一输入，只换 {{STYLE}} ---
        base = await _profile_recipe(runtime.session_factory, frozen["pack"])
        if base is None:
            # 没有语料就不能编一个"来自语料的 recipe"：那会把一次 smoke
            # 变成一个假结论。用一个显式的、标注了来源的替代，并如实记录。
            base = _fallback_recipe()
            report["profile_recipe_available"] = False
            report["profile_recipe_note"] = (
                "库里没有 corpus Profile，Smoke 2 改用一份**显式构造**的 recipe。"
                "它仍然证明'接 recipe 与不接 recipe 的稿子不一样'，"
                "但证明不了'从真实语料推出来的 recipe 有用'——后者需要先采语料。"
            )
        else:
            report["profile_recipe_available"] = True

        # --- Smoke 1：真实 corpus → Profile → recipe（工作单第十一节）---
        # 这一条不是"读一个 recipe"，是把**推导过程本身**记下来：用了多少条
        # 样本、profile_ref 指回哪一版 Profile、每一档的依据是什么。
        # 没有这一条，"recipe 是从真实语料来的"就只是一句声明。
        print(">>> Smoke 1：真实 corpus → Profile → recipe …", flush=True)
        report["smokes"]["1_corpus_to_recipe"] = await _corpus_to_recipe_report(
            runtime.session_factory, frozen["pack"]
        )
        print(
            f"    语料 {report['smokes']['1_corpus_to_recipe'].get('sample_count')} 条样本，"
            f"profile_ref={report['smokes']['1_corpus_to_recipe'].get('profile_ref')!r}",
            flush=True,
        )

        # --- §七 Case A / Case B：同一输入，只换 {{STYLE}} ---
        print(">>> §七 Case A：不接 recipe（基线）…", flush=True)
        report["smokes"]["2_baseline_no_recipe"] = await write_round(llm_service, frozen, None)

        print(">>> Smoke 2（§七 Case B）：接真实 recipe …", flush=True)
        report["smokes"]["2_writer_with_real_recipe"] = await write_round(
            llm_service, frozen, base
        )
        report["smokes"]["2_recipe"] = _recipe_summary(base)
        report["smokes"]["2_vs_baseline"] = _compare(
            report["smokes"]["2_baseline_no_recipe"]["metrics"],
            report["smokes"]["2_writer_with_real_recipe"]["metrics"],
        )

        # --- Smoke 4：换一个 recipe ---
        print(">>> Smoke 4：换一个 recipe …", flush=True)
        report["smokes"]["4_alt_recipe"] = await write_round(llm_service, frozen, ALT_RECIPE)
        report["smokes"]["4_vs_2"] = _compare(
            report["smokes"]["2_writer_with_real_recipe"]["metrics"],
            report["smokes"]["4_alt_recipe"]["metrics"],
        )
        report["smokes"]["4_alt_recipe_recipe"] = _recipe_summary(ALT_RECIPE)

        # --- Smoke 5：没有 StyleRecipe 时 Agent 仍能正常完成 ---
        print(">>> Smoke 5：没有 Profile 时跑完整 Agent …", flush=True)
        report["smokes"]["5_no_recipe_full_run"] = await _no_profile_run(
            runtime, runtime.session_factory, llm_service
        )

        # --- §九：交付候选与真实语料的 n-gram 重叠 ---
        print(">>> §九：交付候选与真实语料的 n-gram 重叠 …", flush=True)
        report["corpus_reuse_check"] = await corpus_reuse_check(
            runtime.session_factory,
            {
                "baseline_no_recipe": report["smokes"]["2_baseline_no_recipe"],
                "writer_with_real_recipe": report["smokes"]["2_writer_with_real_recipe"],
                "alt_recipe": report["smokes"]["4_alt_recipe"],
            },
        )
        reuse = report["corpus_reuse_check"]
        print(
            f"    检查 {reuse['candidates_checked']} 条候选，"
            f"命中 {len(reuse['offenders'])} 条"
            f"（检测器反证：{'会报警' if reuse['detector_self_test']['fires'] else '**不报警**'}）",
            flush=True,
        )

    # --- Smoke 3：确定性（纯离线，但用真实库里的语料）---
    print(">>> Smoke 3：同一 corpus 重复生成 Profile …", flush=True)
    report["smokes"]["3_profile_determinism"] = await corpus_determinism(
        runtime_session_factory
    )

    report["x_requests"] = {
        "total": len(X_REQUESTS),
        "by_method": _method_counts(),
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
    document.setdefault("phase", "5B.2")
    document.setdefault("note", "每次 smoke 一份记录，累积保存在 runs 里。")
    document.setdefault("runs", []).append(report)
    REPORT_PATH.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n记录已写入 {REPORT_PATH}")
    return 0


async def _corpus_to_recipe_report(session_factory, evidence_pack=None) -> dict:
    """Smoke 1：把"真实语料 → Profile → recipe"整条链路的**中间量**记下来。

    只记最终 recipe 是不够的：一个 recipe 看起来永远合理，除非你能看到
    它背后的样本数与分布。所以这里把 Profile 的主要统计量一并落盘，
    让"这个结构是从哪来的"能被人核对。

    **不记任何一条样本原文**——它量的永远是统计量（工作单第四十节）。
    """
    async with session_factory() as session:
        samples = (
            await session.execute(select(StyleSample).where(StyleSample.included.is_(True)))
        ).scalars().all()
        row = (
            await session.execute(
                select(StyleProfile)
                .where(StyleProfile.scope == "corpus")
                .order_by(StyleProfile.computed_at.desc())
                .limit(1)
            )
        ).scalars().first()

    profile = compute_profile(samples)
    recipe = await _profile_recipe(session_factory, evidence_pack)
    return {
        "corpus_samples": len(samples),
        "profile_row_id_present": row is not None,
        "profile": profile.model_dump(),
        "recipe": _recipe_summary(recipe),
        "recipe_reasons": list(recipe.reasons) if recipe else [],
    }


async def _profile_recipe(session_factory, evidence_pack=None) -> StyleRecipe | None:
    """从库里真实的 corpus Profile 推一份 recipe。

    **`sample_count == 0` 也返回 None**，与 `build_recipe` 自己的判据一致
    （见 `app/style/recipe.py:116`）。一个 0 样本的 Profile 行是"采过但一条都
    没达到门槛"的记录，不是一份可以据以定结构的语料——把它当成"有 Profile"
    会让报告写出一句与事实相反的话，而这份报告的全部价值就在于它说的是事实。

    `evidence_pack` 传进来时，recipe 的 hook / 立场 / 证据要求 / 密度会按**这一轮
    真实证据**取档——那正是生产路径的做法（`resolve_recipe` 就是这么调的）。
    不传的话这些档位退化成最保守的一档，那么 Smoke 2 展示的就不是 Writer
    真正收到的那份约束，而"接 recipe 与不接 recipe 有区别"这句话也就少了一半。
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                select(StyleProfile)
                .where(StyleProfile.scope == "corpus")
                .order_by(StyleProfile.computed_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if row is None or not row.sample_count:
            return None
        from app.style.profile import features_from_row

        profile = features_from_row(row)
        computed_at = getattr(row, "computed_at", None)
        return build_recipe(
            profile,
            evidence_signals(evidence_pack),
            profile_ref=f"corpus@{computed_at.isoformat()}" if computed_at else "",
            # 与生产路径同一个来源：候选工具递进去的就是 `persona.max_length`。
            # 不传的话，这份"真实 recipe"会有一个生产里不存在的宽松上限，
            # 于是 smoke 展示的就不是 Writer 实际收到的那份约束。
            hard_max_length=load_persona().max_length,
        )


def _fallback_recipe() -> StyleRecipe:
    """库里没有语料时用的替代 recipe。**显式标注它不是从语料推出来的。**"""
    return StyleRecipe(
        structure_type=StructureType.OPINION_FIRST,
        hook_type=HookType.STATEMENT,
        observed_target_length=180,
        hard_max_length=load_persona().max_length,
        density=DensityLevel.MEDIUM,
        stance_strength=StanceStrength.MODERATE,
        evidence_requirement=EvidenceRequirement.REQUIRED,
        tone=Tone.ANALYTICAL,
        ending_type=EndingType.CONCLUSION,
        reasons=["（smoke 用）库里没有 corpus Profile，这是一份显式构造的 recipe"],
        profile_ref="smoke@no-corpus",
    )


async def _no_profile_run(runtime, session_factory, llm_service) -> dict:
    """Smoke 5：走一遍"没有 Profile"的 fallback，并确认完整运行不受影响。

    两件事分开做，因为它们是两个不同的承诺：

    1. `resolve_recipe(None, ...)` **必须**给回一个中性 recipe，而且渲染出来的
       prompt 里不能残留字面量 `{{STYLE}}`——那是静默失败（`render` 只替换
       传进去的键）。
    2. 整条 Agent 在"没有 recipe 可依据"时仍然跑到人工审核、仍然产出 draft。

    **第 2 步必须真的把 Profile 藏起来。** 库里现在有语料，照直跑一遍的话
    那轮运行拿到的是一份真实 recipe，于是"没有 recipe 时也能跑完"这句话
    根本没被检验到——而它看起来和检过了完全一样。所以这里临时把
    `repositories.get_latest_corpus_profile` 换成"查不到"，让整条生产路径
    （候选工具 → `resolve_recipe` → 中性 recipe → Writer prompt）走一次
    真实的无 Profile 分支，跑完再还原。
    """
    neutral = await resolve_recipe(None, None)
    prompt = build_writer_prompt(
        {
            "angle": type("A", (), {"claim": "（smoke）", "why_it_matters": "（smoke）"})(),
            "topic": "（smoke）",
            "evidence_text": "（smoke）",
        },
        neutral,
    )

    from app import repositories

    original_lookup = repositories.get_latest_corpus_profile

    async def _no_profile(_session):  # noqa: ANN001 - 只为签名匹配
        return None

    repositories.get_latest_corpus_profile = _no_profile  # type: ignore[assignment]
    try:
        async with session_factory() as session:
            run = await create_agent_run(session, None, goal=GOAL)
            run_id = str(run.id)
        state = await runtime.graph.ainvoke(
            {"run_id": run_id, "goal": GOAL},
            config={"configurable": {"thread_id": run_id}},
        )
    finally:
        repositories.get_latest_corpus_profile = original_lookup  # type: ignore[assignment]

    return {
        "neutral_recipe": _recipe_summary(neutral),
        "neutral_prompt_has_no_placeholder": "{{" not in prompt,
        "profile_lookup_hidden_for_this_run": True,
        "run_id": run_id,
        "completed": bool(state.get("draft_id")),
        "steps": state.get("step_count"),
        "finish_reason": state.get("finish_reason"),
        "guardrail_passed": state.get("guardrail_passed"),
        # 真正的证据在这里：这一轮 state 里的 recipe 必须是**中性**的那一份。
        # 如果它长得像真实 recipe，说明藏 Profile 没生效，"跑完了"这件事
        # 就与 fallback 无关。
        "recipe_used": _recipe_summary(state.get("style_recipe")),
        "recipe_was_neutral": _is_neutral(state.get("style_recipe")),
    }


def _is_neutral(recipe) -> bool | None:
    """state 里的 recipe 是不是走 fallback 得到的那一份。

    `structure_type == unclassified` 且没有语料目标长度——产物里唯一可能
    长成这样的就是 `build_recipe(None)`。`None`（压根没有 recipe）也算通过，
    那同样是"没有可依据的 Profile"。
    """
    if recipe is None:
        return True
    return (
        _value(getattr(recipe, "structure_type", None)) == StructureType.UNCLASSIFIED.value
        and getattr(recipe, "observed_target_length", None) is None
    )


def _compare(left: dict, right: dict) -> dict:
    """两组指标的差异。只列**可核对**的字段，不做任何"哪个更好"的判断。"""
    if not left or not right:
        return {}
    keys = [
        "chars_median",
        "sentences_median",
        "paragraphs_median",
        "numbers_per_100_median",
        "concrete_entities_median",
        "questions_total",
        "generic_phrase_hits_total",
    ]
    diff = {key: {"from": left.get(key), "to": right.get(key)} for key in keys}
    diff["openings"] = {"from": left.get("openings"), "to": right.get("openings")}
    diff["closings"] = {"from": left.get("closings"), "to": right.get("closings")}
    return diff


def _method_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in X_REQUESTS:
        counts[entry["method"]] = counts.get(entry["method"], 0) + 1
    return counts


def _print_summary(report: dict) -> None:
    print("\n" + "=" * 68)
    for key, value in report["smokes"].items():
        if key.startswith("1_vs_2") or key.startswith("2_vs_3") or key == "2_recipe":
            continue
        print(f"\n{key}")
        if "metrics" in value:
            print(f"  {json.dumps(value['metrics'], ensure_ascii=False)}")
        elif "identical" in value:
            print(f"  sample_count={value['sample_count']} identical={value['identical']}")
        else:
            print(f"  {json.dumps(value, ensure_ascii=False, default=str)}")
    reuse = report.get("corpus_reuse_check")
    if reuse:
        print("\n§九 语料复用检查")
        print(f"  与 {reuse['corpus_samples']} 条真实样本比对 {reuse['span_n']} 字 n-gram，"
              f"检查 {reuse['candidates_checked']} 条候选，命中 {len(reuse['offenders'])} 条")
        print(f"  检测器反证：{json.dumps(reuse['detector_self_test'], ensure_ascii=False)}")
    print("\n请求：", json.dumps(report["x_requests"], ensure_ascii=False))
    posts = report["x_requests"]["by_method"].get("POST", 0)
    print(f"POST 请求数：{posts}（必须为 0）")


def main() -> int:
    install_read_only_guard()
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
