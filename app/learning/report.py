"""把记录、分组、比较、证据、准入开关组装成一份**经得起追问**的报告（Phase 5C）。

这是纯核心的出口。它做两件事，第二件比第一件重要：

1. 调 `build_cohorts` / `compare_cohorts` / `evaluate_learning_readiness`；
2. **在返回之前把几条纪律检查一遍**，任何一条不满足就抛错：

   - 可追溯（工作单第十八节）：每条证据引用的 snapshot / post id 都真的出现在
     这份报告的记录里；没有依据的结论不许出门。
   - 不声称因果（工作单第二十节）：每条陈述过因果词检查。
   - 不写成指令（工作单第二十节）：每条陈述过指令词检查。
   - `confidence` 恒为 `None`：没有校准过的置信度，编一个数字就是捏造。
   - 诚实交代没算什么（工作单第三十一节）：`limitations` 必填，且由**这份数据
     的具体缺口**推出来，不是一段模板文案。

把检查放在出口而不是散在生成处，理由同 `app/feedback/report.py`：生成处将来会
长出新的比较种类，出口只有一个，漏检的可能性最小。
"""

from __future__ import annotations

from typing import Sequence

from app.feedback.signals import (
    MIN_SAMPLES_FOR_COMPARISON,
    CausalLanguageError,
    assert_no_causal_claim,
    find_instruction_language,
)
from app.learning.cohort import build_cohorts, compare_cohorts
from app.learning.evidence import build_evidence, evaluate_learning_readiness
from app.learning.schema import (
    CohortDimension,
    LearningReport,
    PerformanceMetric,
    PostPerformanceRecord,
)

#: 本阶段要遍历的维度，顺序固定（报告因此是确定性的）。
COHORT_DIMENSIONS: tuple[CohortDimension, ...] = (
    CohortDimension.STRUCTURE_TYPE,
    CohortDimension.HOOK_TYPE,
    CohortDimension.STANCE,
    CohortDimension.LENGTH_BUCKET,
    CohortDimension.COUNTERPOINT_AWARENESS,
    CohortDimension.SCOPE_EXCEEDS_EVIDENCE,
)

#: 本阶段要比较的指标，顺序固定。
COHORT_METRICS: tuple[PerformanceMetric, ...] = (
    PerformanceMetric.ATTENTION,
    PerformanceMetric.CONVERSATION,
    PerformanceMetric.AMPLIFICATION,
)

#: 每一份报告都带着的底线说明。它们是**口径**，不随数据变化。
#: 措辞刻意避开因果连词与指令词——报告里的每一句都要经得起同一套检查。
BASE_LIMITATIONS: tuple[str, ...] = (
    "本报告只描述不同分组在观察到的计数上的差异，不声称任何因果关系。",
    "点赞、回复、转发、引用是四类不同的行为，本报告不把它们合成单一分数。",
    "观测来自公开计数；曝光与收藏从未采集，恒为未知，不按 0 计入。",
    "没有对照组、没有实验设计；任何组间差异都可能来自内容之外的变量。",
    "本报告不产出任何“以后该怎么写”的规则或建议，只保留观察到的信号。",
    "离群点只被标记、不被删除；判据是 IQR 栅栏，在样本接近下限时不稳定。",
)


class LearningReportError(ValueError):
    """报告在出口处没有通过某条纪律检查。"""


def _known_snapshot_ids(records: Sequence[PostPerformanceRecord]) -> set[str]:
    known: set[str] = set()
    for record in records:
        known.update(o.snapshot_id for o in record.observations)
    return known


def _check_wording(text: str, where: str) -> None:
    """一段陈述必须既不含因果词、也不含指令词。

    两个底层守卫各抛自己的异常类型，这里统一折成 `LearningReportError`：
    出口只应该让调用方 catch 一种错误。否则"报告被拒了"这件事会因为触发它的是
    哪种措辞而变成两个不同的异常类型，而调用方多半只会记得 catch 一个。
    """
    try:
        assert_no_causal_claim(text, where=where)
    except CausalLanguageError as exc:
        raise LearningReportError(str(exc)) from exc
    hits = find_instruction_language(text)
    if hits:
        raise LearningReportError(f"{where} 出现指令性措辞 {hits!r}：{text!r}")


def _validate(report: LearningReport) -> None:
    """可追溯 + 无因果 / 无指令 + `confidence` 不得编造。"""
    known_snapshots = _known_snapshot_ids(report.records)
    known_posts = {r.published_post_id for r in report.records}

    for comparison in report.comparisons:
        _check_wording(comparison.statement, where="comparison statement")

    for evidence in report.evidence:
        if evidence.confidence is not None:
            raise LearningReportError(
                f"证据 {evidence.id} 带了一个未经校准的 confidence={evidence.confidence!r}"
            )
        _check_wording(evidence.statement, where=f"evidence {evidence.id}")
        unknown = [sid for sid in evidence.source_snapshot_ids if sid not in known_snapshots]
        if unknown:
            raise LearningReportError(
                f"证据 {evidence.id} 引用了本报告里不存在的观察 {unknown!r}"
            )
        unknown_posts = [pid for pid in evidence.source_post_ids if pid not in known_posts]
        if unknown_posts:
            raise LearningReportError(
                f"证据 {evidence.id} 引用了本报告里不存在的帖子 {unknown_posts!r}"
            )


def _data_limitations(
    records: Sequence[PostPerformanceRecord],
    window: str,
) -> list[str]:
    """由这份数据的具体缺口推出"哪里不足"。"""
    notes: list[str] = []
    n = len(records)
    if n == 0:
        notes.append("没有任何已发布帖子可供观察，因此没有产出任何记录或比较。")
    off_window = sum(1 for r in records if r.observation_window != window)
    if off_window:
        notes.append(
            f"有 {off_window}/{n} 条记录的观察窗口不是 {window}，"
            "它们不进入任何分组，也不参与任何比较。"
        )
    missing_structure = sum(1 for r in records if r.structure_type is None)
    if missing_structure:
        notes.append(
            f"有 {missing_structure}/{n} 条帖子读不到写作结构（checkpoint 缺失或不是 Agent 运行），"
            "这些帖子不进入 structure_type 分组。"
        )
    missing_length = sum(1 for r in records if r.length_bucket is None)
    if missing_length:
        notes.append(
            f"有 {missing_length}/{n} 条帖子没有可用的正文长度，不进入长度档分组。"
        )
    notes.append(
        "未纳入 `topic class` 维度：本仓库没有确定性的选题分类来源，"
        "无法在不引入模型判断的前提下分组。"
    )
    return notes


def build_learning_report(
    records: Sequence[PostPerformanceRecord],
    *,
    window: str,
    min_samples: int = MIN_SAMPLES_FOR_COMPARISON,
) -> LearningReport:
    """记录 → 报告。所有校验在返回前完成。"""
    records = tuple(records)

    cohorts = []
    for dimension in COHORT_DIMENSIONS:
        dimension_cohorts = build_cohorts(records, dimension, window=window)
        cohorts.extend(dimension_cohorts)

    comparisons = []
    for dimension in COHORT_DIMENSIONS:
        dimension_cohorts = [c for c in cohorts if c.dimension is dimension]
        for metric in COHORT_METRICS:
            comparisons.extend(
                compare_cohorts(
                    records,
                    dimension_cohorts,
                    dimension=dimension,
                    window=window,
                    metric=metric,
                    min_samples=min_samples,
                )
            )

    readiness = evaluate_learning_readiness(
        records, cohorts, comparisons, window=window, min_samples=min_samples
    )
    evidence = build_evidence(comparisons, records, readiness)

    limitations = tuple(
        dict.fromkeys(
            (*BASE_LIMITATIONS, *_data_limitations(records, window), *readiness.limitations)
        )
    )

    report = LearningReport(
        observation_window=window,
        records=records,
        cohorts=tuple(cohorts),
        comparisons=tuple(comparisons),
        evidence=evidence,
        readiness=readiness,
        limitations=limitations,
    )
    _validate(report)
    return report


def build_learning_report_offline(
    records: Sequence[PostPerformanceRecord],
    *,
    window: str | None = None,
    min_samples: int = MIN_SAMPLES_FOR_COMPARISON,
) -> LearningReport:
    """离线便利入口：不给窗口时，从记录里挑读数最多的那个窗口。

    `app/learning/runtime.py` 走的是同一条路，只是它的记录来自数据库。
    离线 fixture 用这个函数，于是"数据 → 报告"这一段逻辑在真机与离线之间**共享**。
    """
    records = tuple(records)
    if window is None:
        from app.learning.performance import dominant_window

        window = dominant_window([r.observations for r in records])
    return build_learning_report(records, window=window, min_samples=min_samples)


__all__ = [
    "BASE_LIMITATIONS",
    "COHORT_DIMENSIONS",
    "COHORT_METRICS",
    "LearningReportError",
    "build_learning_report",
    "build_learning_report_offline",
]
