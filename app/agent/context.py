"""话题背景（Context）的两层确定性处理：**边界守卫**与**陌生读者信号**。

## 为什么要有这个模块，而不是只改 prompt

真机故障是结构性的，不是措辞问题：Agent 在研究过程中已经知道"新型云"指什么、
那个 2.2 万星的开源库与它什么关系，于是草稿第一句就把这条**事实**当成了
**话题的入口**——读者第一句就被一个陌生的具体项目拦住，看不出这整篇在讲什么。

"再写一句提示词"解决不了它：提示词是概率性的，而这个故障的两种成分都能用
代码判断——**这段背景有没有出处**、**正文有没有给读者台阶**。所以这里做两件事，
都遵循仓库已有的两条纪律（见 `app/style/signals.py` 与 `app/agent/tools/evidence.py`）：

1. **不信任，只校验**（`sanitize_context`）：模型说这段背景"来自材料"，那它就得
   给出真实的 research_item id；给不出就把来源降级成推断。用户目标里的话可以当
   前提，但**不能带数字**——"今天关注美股"是前提，"市场规模 3000 亿"不是。
2. **信号只作参考事实，绝不当分数**（`context_signals`）：算出来的痕迹印给 Critic
   看，由它自己判断。把痕迹掺进 `context_fit` 会让一个可复算的量与一个不可复算的
   量合成一个既不能追问也不能复核的数。

## 为什么是"最小必要背景"而不是"背景介绍"

Context 不承担说服、不承担论证。它回答的是"读者要能看懂这篇，最少得知道什么"。
一段写得越丰富，越容易变成一篇小作文——而那正是工作单第八节要挡的"新闻稿 /
百科介绍 / 科普文章"。所以 `sanitize_context` 宁可把某个字段整段清空，也不肯
把一段带着无据数字的背景放行：**空着比编一段更诚实**（工作单第六节）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agent.schemas import ContextOrigin, EvidencePack, TopicContext
from app.style.spans import corpus_spans, overlapping_spans, spans_of

#: 背景块与正文"承接关系"的判定粒度。4 字：够短，能抓住"新型云""去中心化 GPU 云"
#: 这类话题自己的词汇；又够长，不至于让"这件事""所以说"这种通用搭配误判成承接。
#: 与 `app/style/spans.py::SHARED_SPAN`（12，判"搬运"）刻意不同：那边判的是抄袭，
#: 这边判的是**有没有接上话**，两个问题的松紧本来就该不一样。
CONTEXT_SPAN = 4

#: 首句"就是在复述一条事实"的判定粒度（复用 12 字这条既有口径）。
#: 比它更松会在"首句提到同一个数字"时误报，那只是共享一个量，不是搬运一条事实。
ENTRY_FACT_SPAN = 12

#: 无据可依的**措辞**：这些说法本身就是结论（"一致认为""众所周知"），
#: 无论有没有出处都不该出现在背景里——背景只负责让读者进门，不负责下判断。
BANNED_PHRASES: tuple[str, ...] = (
    "华尔街一致认为",
    "业内普遍认为",
    "普遍认为",
    "大家一致认为",
    "众所周知",
    "毫无疑问",
    "投资者正在疯狂买入",
    "所有人都在",
    "市场一致预期",
    "已成共识",
)

#: 无据可依的**量化断言**（工作单第五节）：市场规模 XX 亿 / 某公司拥有 XX 万 GPU。
#: 单独判"数字"没意义（"2026 年 3 月"也是数字），所以匹配的是**量级单位**——
#: 一个背景块里冒出"亿/万/千/百/％/%/倍"而它又没有出处，那一定是编的。
_MAGNITUDE = re.compile(r"\d+(?:\.\d+)?\s*(?:万亿|亿|万|千|百|%|％|倍)")
_UNIT_WORD = re.compile(r"(?:万亿|亿|万|千|百)\s*(?:美元|元|人|条|家|台|张|块|个)")

#: 首句里的"具体对象"：带量级的数、拉丁专名（GPU / GPT-5 / vLLM）、引号里的短语。
#: 这三种是读者**进门时最容易被拦下**的东西——它们要么需要解释，要么需要交代。
_QUOTED = re.compile(r"[「『“\"]([^」』”\"]{2,20})[」』”\"]")
_LATIN_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-._][A-Za-z0-9]+)*")

_SENTENCE_END = re.compile(r"(?<=[。！？!?；;\n])")


def sentences(text: str) -> list[str]:
    """按中文句读切句（不引第三方分词）。空文本给空列表。"""
    return [part.strip() for part in _SENTENCE_END.split(text or "") if part.strip()]


def first_sentence(text: str) -> str:
    """正文的第一句——读者**唯一**的入口。"""
    parts = sentences(text)
    return parts[0] if parts else ""


def is_empty(context: TopicContext | None) -> bool:
    """这段背景有没有实质内容。

    **四个字段都空**才算空：只填了 `subject` 的一份背景仍然有用（它就是
    "这篇在讲什么"）。而 `origin` / `source_ids` 是元数据，不参与这个判断。
    """
    if context is None:
        return True
    return not any(
        (getattr(context, name) or "").strip()
        for name in ("subject", "definition", "why_now", "core_tension")
    )


def context_text(context: TopicContext | None) -> str:
    """背景的正文（不含来源声明），供承接检测与 prompt 渲染复用。

    顺序固定为 subject → definition → why_now → core_tension。**没有随机、
    没有条件分支**：同一份 context 今天和三个月后拼出同一段文本，
    这是下面所有 n-gram 判定可复算的前提。
    """
    if context is None:
        return ""
    parts = [
        (getattr(context, name) or "").strip()
        for name in ("subject", "definition", "why_now", "core_tension")
    ]
    return "\n".join(part for part in parts if part)


def find_unsupported_claims(text: str) -> list[str]:
    """这段文字里无据可依的措辞与量化断言，逐字回传（便于核对误报）。

    量化断言**一律**算命中：本函数没有"有没有出处"这个参数，因为调用方
    （`sanitize_context`）只有在 `origin=fact` **且** 出处核验通过时才会
    跳过这一项——把那个判断放在这里会让一个纯函数依赖外部状态。
    """
    body = text or ""
    hits = [phrase for phrase in BANNED_PHRASES if phrase in body]
    hits.extend(match.group(0) for match in _MAGNITUDE.finditer(body))
    hits.extend(match.group(0) for match in _UNIT_WORD.finditer(body))
    return hits


def sanitize_context(
    context: TopicContext | None, known_source_ids: set[str]
) -> tuple[TopicContext | None, list[str]]:
    """把模型给的背景修剪成**只保留能站住的部分**，返回 (背景, 处置说明)。

    四条规则，每条都对应工作单里一句话：

    1. `origin=fact` 的出处必须落在本次真实的 research_item id 上；过滤后一条不剩
       ⇒ **降级为 `inference`**（工作单第五节：推断必须标成推断）。
    2. 非 `fact` 的 `source_ids` 一律清空——用户前提与推断没有"出处"这回事。
    3. 逐句跑禁用词与量化断言；命中**整句丢掉**（不是整段丢，也不是改写成模糊说法）。
       只有 `fact` **且** 出处核验通过时才豁免量化断言。
    4. 清完若四个字段都空 ⇒ 返回 `None`：本次没有可用背景，下游按"没有背景"渲染。

    返回值里的说明会进 tool summary 与日志。**丢弃是可见的**——静默修剪会让
    "模型有多常在编背景"这个真实故障率藏起来（同 `tools/evidence.py::_keep_traceable`）。
    """
    if context is None:
        return None, []

    notes: list[str] = []
    origin = context.origin

    valid_ids = [sid for sid in context.source_ids if sid in known_source_ids]
    if origin is ContextOrigin.FACT:
        if not valid_ids:
            origin = ContextOrigin.INFERENCE
            notes.append("背景声称来自材料，但出处核验不上，已降级为推断")
    elif context.source_ids:
        notes.append("非事实来源的背景不带出处，已清空 source_ids")
        valid_ids = []

    backed = origin is ContextOrigin.FACT
    updates: dict[str, object] = {"origin": origin, "source_ids": valid_ids}
    for name in ("subject", "definition", "why_now", "core_tension"):
        original = (getattr(context, name) or "").strip()
        if not original:
            continue
        kept: list[str] = []
        dropped: list[str] = []
        for sentence in sentences(original):
            hits = find_unsupported_claims(sentence)
            # 有出处的事实性背景可以带数字：材料里写了"2.2 万星"，它就不是编的。
            if backed:
                hits = [hit for hit in hits if hit in BANNED_PHRASES]
            if hits:
                dropped.append(sentence)
                notes.append(f"{name} 去掉一句无据表述（{'、'.join(hits)}）：{sentence}")
                continue
            kept.append(sentence)
        updates[name] = "".join(kept)

    cleaned = context.model_copy(update=updates)
    if is_empty(cleaned):
        return None, notes
    return cleaned, notes


#: 首句里最多回传几个具体对象。回传是为了便于核对误报，不是为了完整——
#: 一屏塞不下的清单会让真正异常的那一条淹没在里面（同 `format_signals`）。
MAX_ENTRY_SPECIFICS = 4


@dataclass(frozen=True)
class ContextSignals:
    """一条候选的**确定性**陌生读者信号。

    三项都是"可以复算的痕迹"，不是判断：同一条稿子今天和三个月后算出同一个结果，
    被质疑时可以重新核对。真正的分数仍由 Critic 给（`context_fit`）。
    """

    candidate_id: str = ""
    #: 正文全文与背景**没有任何** 4 字以上共同片段 ⇒ 整篇没接住背景。
    #: 注意这与"写得好不好"无关：它只说明"这篇没有用到本话题自己的词汇"。
    context_missing: bool = False
    #: 首句抛出了一个背景里没解释过的具体对象（带量级的数 / 专名 / 引号短语）。
    concept_jump: bool = False
    #: 首句就是在复述一条事实（12 字以上连续重合），而话题本身还没被交代出来。
    #: 这就是工作单第十二节的"证据劫持"：把事实当成话题的入口。
    evidence_hijacking: bool = False
    #: 首句里那些具体对象，逐字回传。
    entry_specifics: list[str] = field(default_factory=list)
    #: 首句复述到的那条事实编号（如有）。
    hijacked_fact_id: str = ""


def _entry_specifics(first: str) -> list[str]:
    """首句里的具体对象：引号短语、量级数字、拉丁专名。去重且保持出现顺序。"""
    found: list[str] = []
    for pattern in (_QUOTED, _MAGNITUDE, _LATIN_NAME):
        for match in pattern.finditer(first or ""):
            token = (match.group(1) if match.re is _QUOTED else match.group(0)).strip()
            if token and token not in found:
                found.append(token)
    return found[:MAX_ENTRY_SPECIFICS]


def context_signals(
    context: TopicContext | None,
    candidates: list,
    pack: EvidencePack | None = None,
) -> list[ContextSignals]:
    """逐条候选算陌生读者信号。纯函数：无 IO、无随机、不调 LLM。

    **背景为空时一律不报**：没有背景可承接，就谈不上"没接住"。
    在那种运行里（老 checkpoint、材料不足）判出"缺背景"只会是噪声，
    而噪声会让真是问题的那一条失去意义。
    """
    if is_empty(context):
        return []

    body_text = context_text(context)
    body_spans = corpus_spans([body_text], CONTEXT_SPAN)
    facts = list(getattr(pack, "facts", None) or [])
    fact_spans = {
        fact.fact_id: spans_of(fact.fact, ENTRY_FACT_SPAN) for fact in facts
    }

    signals: list[ContextSignals] = []
    for candidate in candidates:
        content = getattr(candidate, "content", "") or ""
        first = first_sentence(content)
        entry_spans = spans_of(first, CONTEXT_SPAN)

        # 承接：整篇有没有和背景共享过任何 4 字片段。
        context_missing = not overlapping_spans(content, body_spans, CONTEXT_SPAN)

        # 首句抛出背景里没解释过的具体对象 ⇒ 概念跳步。
        specifics = _entry_specifics(first)
        concept_jump = bool(specifics) and not overlapping_spans(
            first, body_spans, CONTEXT_SPAN
        )

        # 首句复述了一条事实，而首句里没有话题自己的词汇 ⇒ 证据劫持。
        hijacked = ""
        if first:
            for fact in facts:
                if overlapping_spans(first, fact_spans[fact.fact_id], ENTRY_FACT_SPAN):
                    hijacked = fact.fact_id
                    break
        evidence_hijacking = bool(hijacked) and not (
            entry_spans & body_spans
        )

        signals.append(
            ContextSignals(
                candidate_id=getattr(candidate, "candidate_id", "") or "",
                context_missing=context_missing,
                concept_jump=concept_jump,
                evidence_hijacking=evidence_hijacking,
                entry_specifics=specifics,
                hijacked_fact_id=hijacked if evidence_hijacking else "",
            )
        )
    return signals


__all__ = [
    "BANNED_PHRASES",
    "CONTEXT_SPAN",
    "ContextSignals",
    "context_signals",
    "context_text",
    "find_unsupported_claims",
    "first_sentence",
    "is_empty",
    "sanitize_context",
    "sentences",
]
