"""内容质量的汇总视图：把自动指标、人工标注、编辑距离摆在一起。

## 自动指标与人工标注**不混用**

工作单第八节定义了 8 个人工评估维度（specificity / stance / evidence binding /
generic language / template likeness / hook strength / information density /
human edit distance）。这一版**不接生产、也不假装自动算得出来**——所以：

- `dimensions` 里的值**只**来自人工标注。没标注就是 `unavailable`。
- 自动指标放在 `automatic` 里，用**自己的**量纲（密度、计数、比例）。
- 两边不互相填充、不互相替代。想看"自动指标能不能代替人工"，
  那是下一步的验证工作，不是这一版的默认假设。

放一起是为了**对照**：`automatic.concrete_entity_count` 高的稿子，
人工 `specificity` 应该也高。如果两者长期不相关，说明自动指标选错了——
这正是这一层将来要回答的问题，而它要求两边从一开始就是分开记的。

## Human Edit Distance 是特例

它既不是自动指标也不是主观评分：它是**可精确计算**的，只是需要人工改写稿
作为输入。所以它既不进 `automatic`（它不是从草稿本身算的），
也不进 `dimensions`（它不需要人主观打分，只需要人动手改）。单独一栏。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from evals.content.ai_style import AiStyleReport, analyze_style
from evals.content.ai_style import human_edit_distance
from evals.model import ContentQualityRecord
from evals.values import UNAVAILABLE, MetricValue, is_available

#: 工作单第八节的八个人工维度 → `ContentQualityRecord` 的字段名。
#: 用元组而不是散着写，是为了让"报告漏了一个维度"能在自检里被发现。
HUMAN_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("specificity", "specificity"),
    ("stance", "stance"),
    ("evidence_binding", "evidence_binding"),
    ("generic_language", "generic_language"),
    ("template_likeness", "template_likeness"),
    ("hook_strength", "hook_strength"),
    ("information_density", "information_density"),
    ("human_edit_distance", "human_edit_distance"),
)

#: 自动指标的键 → AiStyleReport 的字段名。同样成对登记。
AUTOMATIC_METRICS: tuple[tuple[str, str], ...] = (
    ("char_count", "char_count"),
    ("sentence_count", "sentence_count"),
    ("avg_sentence_length", "avg_sentence_length"),
    ("sentence_length_stdev", "sentence_length_stdev"),
    ("generic_phrase_count", "generic_phrase_count"),
    ("hedge_count", "hedge_count"),
    ("concrete_entity_count", "concrete_entity_count"),
    ("number_count", "number_count"),
    ("concrete_entity_density", "concrete_entity_density"),
    ("question_count", "question_count"),
    ("exclamation_count", "exclamation_count"),
    ("repeated_pattern_count", "repeated_pattern_count"),
    ("repeated_opener_count", "repeated_opener_count"),
    ("abstract_noun_density_approx", "abstract_noun_density_approx"),
)


class ContentQualityReport(BaseModel):
    """一条正文的内容质量观察。**没有 gate，也没有综合分。**"""

    model_config = ConfigDict(extra="forbid")

    available: bool = False
    #: 自动指标（确定性、可复现）。
    automatic: dict[str, MetricValue] = Field(default_factory=dict)
    #: 人工维度（只有标注过才有值）。
    dimensions: dict[str, MetricValue] = Field(default_factory=dict)
    #: 未标注的人工维度。单列出来，好让人一眼看出这份报告还缺多少人工输入。
    unannotated: list[str] = Field(default_factory=list)
    #: 归一化编辑距离（有人工改写稿时才有值）。
    human_edit_distance: MetricValue = UNAVAILABLE
    #: 细微但重要的细节：命中的套话原文、去重后的具体实体、重复片段。
    #: 数字背后的**证据**——没有它，报告只能告诉人"套话 3 个"，不能告诉人"哪三个"。
    details: dict[str, list[str]] = Field(default_factory=dict)
    #: 本模块里哪些指标是近似的，别单独用。
    approximate_metrics: list[str] = Field(default_factory=list)
    note: str = ""


def build_report(
    draft: str,
    *,
    annotation: ContentQualityRecord | None = None,
    style: AiStyleReport | None = None,
    edited: str | None = None,
) -> ContentQualityReport:
    """把草稿 + 可选的标注 + 可选的人工改写稿合成一份内容质量报告。

    `annotation` 与 `edited` 双双缺省是整个 Evaluation Lab 的常态：本阶段不产生
    人工真值，报告如实显示 8 个维度全部 `unavailable`，而不是替人填一个 0。

    `edited` 与 `annotation.human_edit_distance` 的分工：前者是**原始素材**
    （人工改写后的正文），由报告现算距离；后者是**已经算好/人直接给的分**。
    两者同时存在时以后者为准——人给的分是判断，机器算的是度量，
    而当人明确给了一个数，说明他知道自己想要什么口径。
    """
    annotation = annotation or ContentQualityRecord()
    style = style or analyze_style(draft)

    automatic = {name: getattr(style, field) for name, field in AUTOMATIC_METRICS}
    dimensions = {
        name: (getattr(annotation, field) if getattr(annotation, field) is not None else UNAVAILABLE)
        for name, field in HUMAN_DIMENSIONS
    }

    declared = annotation.human_edit_distance
    computed = human_edit_distance(draft, edited)
    edit_distance: MetricValue = declared if declared is not None else computed
    # 两个来源都有值时，把差异显式记下来——它会暴露"人给的分数与文本实际
    # 差多少"不一致，那是标注口径的问题，不该被静默覆盖。
    if declared is not None and edited is not None and computed != UNAVAILABLE:
        note_extra = f"；人工给分 {declared}，由改写稿现算 {computed:.3f}"
    else:
        note_extra = ""
    dimensions["human_edit_distance"] = edit_distance

    note = "自动指标与人工维度量纲不同，不可互相替代" + note_extra
    if not any(is_available(v) for v in dimensions.values()):
        note += "；本次没有任何人工标注，dimensions 全为 unavailable"

    return ContentQualityReport(
        available=style.available,
        automatic=automatic,
        dimensions=dimensions,
        unannotated=[name for name, value in dimensions.items() if not is_available(value)],
        human_edit_distance=edit_distance,
        details={
            "generic_phrases": style.generic_phrases,
            "concrete_entities": style.concrete_entities,
            "repeated_patterns": style.repeated_patterns,
        },
        approximate_metrics=list(style.approximate_metrics),
        note=note,
    )


__all__ = [
    "AUTOMATIC_METRICS",
    "HUMAN_DIMENSIONS",
    "ContentQualityReport",
    "build_report",
    "human_edit_distance",
]
