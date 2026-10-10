"""extract_evidence：把研究材料提炼成可追溯的 EvidencePack。

本文件是整个 Phase 5A 里**最该被怀疑**的一步。工作单第九节的要求是
"事实必须能够追溯到 research source"——而让 LLM 输出一个 `source_ids` 字段，
和一个真的可追溯的事实，是两回事：模型完全可以一边写出 `["abc123"]` 一边让
那个 id 根本不存在。

所以这里做的是**校验而不是信任**：每条事实的 source_ids 都要落在本次运行真实
落库的 research_item id 集合里，引用不到真实材料的事实**直接丢弃**。

丢而不是留（带个标记之类的）是刻意的：一条无法追溯的"事实"，从它连不上任何
研究材料的那一刻起，就已经退化成"模型记得有这么回事"——而那正是工作单禁止的
"从帖子印象生成观点"。留在包里只会让它有机会被 Angle 引用。

丢完之后包可能是空的，这**不是**错误路径：Planner 会看到 `unknowns` 与空事实，
按预算选择继续深化研究或收尾。

## 话题背景也在这里产出（工作单第三节）

"读者要能看懂这篇，最少得知道什么"——这个问题只有**手里握着这批材料**的那一步
答得出来，所以它跟着同一次调用一起产出，存在 `EvidencePack.context` 上。
这一步**不额外调一次 LLM、不额外占一步预算、不多花一次 research_call**：
真的再开一个 "Context Agent" 会新增一个 Graph 节点、一份 prompt、一次失败点，
而它要的输入在这一步已经全在手上。

`context` 与 `facts` 共用同一个 `known` 集合做核验，理由也一样：模型说这段背景
"来自材料"，那它就得给出真实的材料 id；给不出就把来源降级成推断
（见 `app/agent/context.py::sanitize_context`）。
"""

import logging

from app.agent import viewpoint
from app.agent.context import sanitize_context
from app.agent.prompts import format_sources, load_prompt, render
from app.agent.schemas import EvidenceFact, EvidencePack
from app.agent.tools.base import (
    ToolResult,
    complete_json,
    known_source_ids,
    sources,
)
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


def _keep_traceable(pack: EvidencePack, known: set[str]) -> tuple[EvidencePack, int]:
    """丢弃追溯不到真实材料的事实，重新编号并汇总来源。

    返回 (规范化后的证据包, 被丢弃的事实数)。事实数进 trace：如果一次运行里
    大部分事实都被丢掉，那说明这次检索拿回的材料和抽出来的事实对不上，
    这是要看得到的信号，不该静默消失。
    """
    kept: list[EvidenceFact] = []
    dropped = 0
    for fact in pack.facts:
        valid = [sid for sid in fact.source_ids if sid in known]
        if not valid:
            dropped += 1
            continue
        # fact_id 由这里重新编号，不采信模型给的值：让它自己编号会得到重复或跳号，
        # 而 Angle 要按 id 引用它们，引用错了整条证据链就断了。
        kept.append(fact.model_copy(update={"fact_id": f"f{len(kept) + 1}", "source_ids": valid}))

    union = sorted({sid for fact in kept for sid in fact.source_ids})
    return pack.model_copy(update={"facts": kept, "source_ids": union}), dropped


def make_evidence_tool(llm_service: LLMService):
    async def extract_evidence(state, decision) -> ToolResult:
        run_id = state["run_id"]
        material = sources(state)
        known = known_source_ids(state)
        logger.info("agent evidence started (run_id=%s, sources=%d)", run_id, len(material))

        prompt = render(
            load_prompt("evidence"),
            GOAL=state.get("goal") or state.get("topic") or "",
            TOPIC=state.get("selected_topic") or "",
            RESEARCH=format_sources(material),
        )
        parsed: EvidencePack = await complete_json(llm_service, prompt, EvidencePack)
        pack, dropped = _keep_traceable(parsed, known)

        # 话题背景与事实同一趟产出（**不额外调一次 LLM、不额外占一步预算**）：
        # "读者要能看懂，最少得知道什么"这件事，只有手里握着这批材料的那一步答得出来。
        # 清洗规则见 app/agent/context.py——声称来自材料就得给出真实出处，
        # 无出处的量级数字与"业内普遍认为"这类措辞整句丢掉。
        context, context_notes = sanitize_context(parsed.context, known)
        pack = pack.model_copy(update={"context": context})

        logger.info(
            "agent evidence completed (run_id=%s, facts=%d, dropped=%d, context=%s)",
            run_id,
            len(pack.facts),
            dropped,
            "yes" if context is not None else "none",
        )
        summary = (
            f"形成证据包：{len(pack.facts)} 条可追溯事实、"
            f"{len(pack.consensus)} 条共识、{len(pack.counterpoints)} 条反方、"
            f"{len(pack.unknowns)} 处未知"
        )
        # 工作单第五节：事实条数与**独立支撑**是两件事，必须分开报。
        # 1 篇帖子可以拆出 6 条事实，但它们互相之间并不独立——"6 条证据"这个
        # 说法本身在暗示有 6 份材料在托着同一个判断。独立支撑取
        # min(材料数, 作者数)：同一作者的多条来源不是第二份独立来源。
        # 这个数也是 `viewpoint.accept_ceiling_for` 判动机断言时的依据，
        # 印在 summary 里才能让"为什么这条被拦下"在 trace 上看得出来。
        index = viewpoint.evidence_index(
            pack,
            state.get("research_item_ids") or [],
            state.get("research_results") or [],
        )
        bound = viewpoint.bound_sources_of(
            [fact.fact_id for fact in pack.facts], index
        )
        authors, missing_authors = viewpoint.authors_of(bound, index)
        summary += (
            f"；独立支撑 {min(len(bound), len(authors))} 条"
            f"（{len(bound)} 条材料 / {len(authors)} 位作者）"
        )
        if missing_authors:
            summary += f"，另有 {missing_authors} 条材料没有作者信息（不计入作者数）"
        if dropped:
            # 单独写出来。这一条比"形成 N 条事实"重要得多：
            # 它说明模型编了一批出处，是 prompt 或材料需要调整的信号。
            summary += f"；丢弃 {dropped} 条追溯不到来源的事实"
        if context is None:
            summary += "；未能形成话题背景（背景宁可空着也不编）"
        else:
            summary += "；已形成话题背景"
        if context_notes:
            # 同 "丢弃 N 条" 的理由：修剪必须可见，否则"模型有多常在编背景"
            # 这个真实故障率会被静默藏起来。
            summary += f"；背景修剪 {len(context_notes)} 处"
            for note in context_notes:
                logger.info("agent context trimmed (run_id=%s): %s", run_id, note)
        return ToolResult(delta={"evidence_pack": pack}, summary=summary)

    return extract_evidence
