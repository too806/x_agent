"""对样本做**语义**标注（Phase 5B.2 工作单第十三、十四节）。

## 只做语义，不做确定性

长度、段落、emoji、数字、英文比例这些一律由 `features.py` 在 Python 里算，
**不问模型**。这里只回答模型才回答得了的那部分：这条帖子属于哪一类结构、
开头用的哪种钩子、有没有主张/证据/反方/问题/清单/结论。

这条分界是刻意的：确定性指标可复现、可回归、可被质疑时重算；语义标签
不可复现，所以必须**留痕**（模型名、版本、时间），并且只准输出结构化 JSON。

## 为什么禁止自由文本"风格分析"

一段"这条帖子写得很自然，有节奏感"的文字：不可比较、不可聚合、不可回归，
而且它会诱使下游把它当成真值。所以 `StyleAnnotation` 是一个 `extra="forbid"`
的 pydantic 模型，模型返回任何多余字段都会被拒——**宁可失败，不要一段散文**。

## 只在采集脚本里调用

工作单第三十六节：style extraction 应当 offline / 预计算。本模块不在 Agent
运行期被调用；它由采集脚本在入库时跑一次，结果写进 `style_sample` 的标注列，
带 `annotation_model` / `annotation_version` / `annotated_at` 留痕。
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools.base import complete_json
from app.services.llm import LLMService
from app.style.taxonomy import (
    ContentType,
    HookType,
    StructureType,
    coerce_label,
)

#: 标注 prompt 的版本。改了 prompt 或字段含义就要进位——否则两个版本的
#: 标注会被当成同口径混在一起统计。
ANNOTATION_VERSION = "1.0"


class StyleAnnotation(BaseModel):
    """LLM 对一条样本的语义标注结果。**结构化，不是散文。**

    `extra="forbid"`：模型返回没声明的字段就直接失败。这是刻意的——
    宽松地收下多余字段会让"标注 schema 到底是什么"逐渐失去答案，
    而下游统计会开始悄悄依赖一个从没被定义过的字段。
    """

    model_config = ConfigDict(extra="forbid")

    content_type: ContentType = ContentType.UNCLASSIFIED
    structure_type: StructureType = StructureType.UNCLASSIFIED
    hook_type: HookType = HookType.UNCLASSIFIED

    has_claim: bool = False
    has_evidence: bool = False
    has_counterpoint: bool = False
    has_question: bool = False
    has_list: bool = False
    has_number: bool = False
    has_conclusion: bool = False

    #: 模型对这次标注的自评（0-1）。可空——不给自己不确定的标注编一个数。
    confidence: float | None = Field(None, ge=0.0, le=1.0)


def build_annotation_prompt(content: str) -> str:
    """渲染标注 prompt。

    把分类法的**定义**一起给模型，而不是只给成员名：一个只看到
    `fact_first` 这个词的模型会按自己的理解归类，于是 taxonomy 的收敛
    被模型的先验悄悄决定——而我们要的是按**我们定义的**类别分。
    """
    structures = "\n".join(
        f"- {member.value}：{_STRUCTURE_DEFINITIONS[member.value]}" for member in StructureType
    )
    hooks = "\n".join(f"- {member.value}：{_HOOK_DEFINITIONS[member.value]}" for member in HookType)
    contents = "\n".join(
        f"- {member.value}：{_CONTENT_DEFINITIONS[member.value]}" for member in ContentType
    )
    return _ANNOTATION_TEMPLATE.format(
        content=content, structures=structures, hooks=hooks, contents=contents
    )


async def annotate_sample(llm_service: LLMService, content: str) -> StyleAnnotation:
    """标注一条样本。

    模型的原始标签先过 `coerce_label`：它可能返回近义写法，也可能返回一个
    不存在的类别。收不下就落到 `unclassified` —— **不抛异常**。
    为一条样本的标签写法让整批采集失败，代价远大于少归一类。
    """
    prompt = build_annotation_prompt(content)
    result = await complete_json(llm_service, prompt, StyleAnnotation)
    assert isinstance(result, StyleAnnotation)  # complete_json 已校验，这里只为类型收窄
    return _normalize(result)


def _normalize(annotation: StyleAnnotation) -> StyleAnnotation:
    """把模型给的枚举值收进分类法。

    pydantic 已经按枚举校验过一遍，但 `coerce_label` 仍然跑一次是刻意的：
    将来若把枚举校验放宽（比如允许字符串以便接受新标签），这里就是唯一
    的归一化点，不必再去每个调用点补。
    """
    return annotation.model_copy(
        update={
            "structure_type": StructureType(
                coerce_label(StructureType, annotation.structure_type)
            ),
            "hook_type": HookType(coerce_label(HookType, annotation.hook_type)),
            "content_type": ContentType(coerce_label(ContentType, annotation.content_type)),
        }
    )


def annotation_timestamp() -> datetime:
    """标注时刻。单独一个函数，方便测试把它钉死。"""
    return datetime.now(timezone.utc)


_ANNOTATION_TEMPLATE = """你是中文科技内容的结构分析器。判断下面这条帖子的**组织方式**。

只输出 JSON，不要解释、不要 Markdown 代码块。

正文：
\"\"\"
{content}
\"\"\"

structure_type（这条帖子怎么组织的，选一个）：
{structures}

hook_type（开头用什么抓住读者，选一个）：
{hooks}

content_type（这条帖子大体是什么性质，选一个）：
{contents}

另外判断这几个有无（true/false）：
- has_claim：有没有一个明确的主张或判断
- has_evidence：有没有可核验的事实、数据、来源
- has_counterpoint：有没有提出或回应反方看法
- has_question：有没有问句
- has_list：有没有分点/清单式表达
- has_number：有没有具体数字
- has_conclusion：有没有明确收尾的结论

JSON 结构必须严格为：
{{"content_type": "...", "structure_type": "...", "hook_type": "...",
  "has_claim": false, "has_evidence": false, "has_counterpoint": false,
  "has_question": false, "has_list": false, "has_number": false,
  "has_conclusion": false, "confidence": 0.0}}

归类时按**组织方式**判，不要按话题判。归不进任何一类就选 unclassified——
选一个"看起来最接近"的类别会污染分布，而 unclassified 会如实显示成"没归进去"。
"""


_STRUCTURE_DEFINITIONS: dict[str, str] = {
    StructureType.OPINION_FIRST.value: "先给判断，再给理由",
    StructureType.FACT_FIRST.value: "先摆事实/数据，再引出判断",
    StructureType.QUESTION_ANSWER.value: "提出一个问题，然后回答它",
    StructureType.CONTRAST.value: "对比 A 与 B，落在一个判断上",
    StructureType.HOW_TO.value: "步骤/清单式，教怎么做",
    StructureType.CASE_REVIEW.value: "一个具体案例的复盘",
    StructureType.COUNTERINTUITIVE.value: "先立一个常见看法，再反驳它",
    StructureType.OBSERVATION.value: "描述现象/趋势，少下结论",
    StructureType.DATA_READING.value: "围绕一组数字展开",
    StructureType.PREDICTION.value: "基于现状推演未来",
    StructureType.UNCLASSIFIED.value: "归不进以上任何一类",
}

_HOOK_DEFINITIONS: dict[str, str] = {
    HookType.QUESTION.value: "用一个问题开头",
    HookType.STATISTIC.value: "用一个数字/数据开头",
    HookType.STATEMENT.value: "用一句直接陈述开头",
    HookType.LIST.value: "用「几条」式的清单预告开头",
    HookType.STORY.value: "用一个具体事件/场景开头",
    HookType.CONTRAST.value: "用一个反差/意外开头",
    HookType.QUOTE.value: "用一句原话或具体指称开头",
    HookType.UNCLASSIFIED.value: "归不进以上任何一类",
}

_CONTENT_DEFINITIONS: dict[str, str] = {
    ContentType.OPINION.value: "观点表达",
    ContentType.ANALYSIS.value: "分析解读",
    ContentType.NEWS.value: "资讯播报",
    ContentType.TUTORIAL.value: "教程/方法",
    ContentType.EXPERIENCE.value: "个人经验/实践",
    ContentType.ANNOUNCEMENT.value: "产品/事件公告",
    ContentType.UNCLASSIFIED.value: "归不进以上任何一类",
}


__all__ = [
    "ANNOTATION_VERSION",
    "StyleAnnotation",
    "annotate_sample",
    "annotation_timestamp",
    "build_annotation_prompt",
]
