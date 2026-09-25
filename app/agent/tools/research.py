"""research_topic / deepen_research：取回研究材料。

两者的区别只有一个：**检索词从哪来**。

- `research_topic` 围绕 Planner 选定的候选选题检索；
- `deepen_research` 针对**已知的空白**检索——优先用检索词的 arguments.query，
  其次是证据包里记下的 unknowns / counterpoints。

这个区别是工作单第九节那句"如果证据不足：Planner 应选择 deepen_research"的落点：
深化研究必须朝"还不知道什么"去，而不是把同一个词再搜一遍——再搜一遍只会拿回
同一批帖子，然后证据包看起来更厚了，实际上一条新事实都没有。
"""

import logging

from app.agent.budget import counter
from app.agent.prompts import format_sources, load_prompt, render
from app.agent.tools.base import ToolResult, append_research, complete_json, pairs
from app.repositories import create_research_items, set_run_topic
from app.services.llm import LLMService
from app.services.x_service import XService

logger = logging.getLogger(__name__)

RESEARCH_RESULTS = 10


def _choose_topic(state, decision) -> tuple[str, str]:
    """决定研究哪个题目，返回 (题目, 说明怎么选出来的)。

    Planner 的 target 只有在**确实是候选之一**时才被采信。不采信一个不存在的
    题目名（可能是模型记错或改了措辞），是因为按它去搜索会得到一批与任何候选都
    对不上的材料，而证据阶段再拿这些材料去支撑观点，整条链就脱离了 topic 候选集。
    回退到置信度最高的候选，并在 trace 里写明发生了回退。
    """
    candidates = state.get("candidate_topics") or []
    names = [c.topic for c in candidates]
    if decision.target and decision.target in names:
        return decision.target, "按决策选定"

    if candidates:
        best = max(candidates, key=lambda c: c.confidence)
        if decision.target:
            return best.topic, f"决策给出的「{decision.target}」不在候选列表里，回退到置信度最高的"
        return best.topic, "决策未指定题目，取置信度最高的候选"

    # 一个候选都没有：用目标本身兜底，至少让这一轮取回的材料与目标相关。
    return str(state.get("goal") or state.get("topic") or ""), "无候选选题，用目标文本兜底"


def make_research_tool(x_service: XService, session_factory):
    async def research_topic(state, decision) -> ToolResult:
        run_id = state["run_id"]
        topic, how = _choose_topic(state, decision)
        logger.info("agent research started (run_id=%s, topic=%r)", run_id, topic)

        results = await x_service.research(topic, max_results=RESEARCH_RESULTS)
        async with session_factory() as session:
            item_ids = await create_research_items(session, run_id, results)
            # 把题目镜像到 Run 上供运行记录列表显示。同一个 session 里做掉，
            # 少一次连接往返；`set_run_topic` 只在 topic 还是空时写入。
            await set_run_topic(session, run_id, topic)

        delta = append_research(state, results, item_ids)
        delta["selected_topic"] = topic
        delta["research_count"] = counter(state, "research_count") + 1
        logger.info("agent research completed (run_id=%s, items=%d)", run_id, len(results))
        return ToolResult(
            delta=delta,
            summary=f"{how}主题「{topic}」，取回 {len(results)} 条材料",
        )

    return research_topic


def _deepen_query(state, decision) -> tuple[str, str]:
    """深化研究的检索词，返回 (检索词, 说明)。

    优先级：Planner 明确给的 query → 证据包里的 unknowns → counterpoints → 原题。
    """
    if decision.arguments.get("query"):
        return str(decision.arguments["query"]), "按决策指定的检索词"

    pack = state.get("evidence_pack")
    if pack is not None:
        if pack.unknowns:
            return pack.unknowns[0], "针对证据里记下的未知项"
        if pack.counterpoints:
            return pack.counterpoints[0], "针对证据里记下的反方说法"

    topic = str(state.get("selected_topic") or "")
    return topic, "没有已知空白，按原题再检索一轮"


def make_deepen_tool(x_service: XService, session_factory):
    async def deepen_research(state, decision) -> ToolResult:
        run_id = state["run_id"]
        query, how = _deepen_query(state, decision)
        logger.info("agent deepen started (run_id=%s, query=%r)", run_id, query)

        results = await x_service.research(query, max_results=RESEARCH_RESULTS)
        async with session_factory() as session:
            item_ids = await create_research_items(session, run_id, results)

        delta = append_research(state, results, item_ids)
        delta["research_count"] = counter(state, "research_count") + 1
        logger.info("agent deepen completed (run_id=%s, items=%d)", run_id, len(results))
        return ToolResult(
            delta=delta,
            summary=f"{how}检索「{query}」，追加 {len(results)} 条材料",
        )

    return deepen_research
