"""discover_topics：从目标出发发现若干候选选题。

工作单第七节：**第一版不建 Trend Intelligence 数据仓库**，先用现有的 X Research 能力。
所以这里就是"搜一批 → 让模型从中读出值得讨论的题目"，不引入任何新的数据源。

搜索结果照常落 `research_item`：发现阶段取回的材料同样是证据的候选来源，
丢掉它们会让 Evidence 阶段少一批可引用的出处。
"""

import logging

from app.agent.budget import counter
from app.agent.prompts import format_sources, load_prompt, render
from app.agent.schemas import TopicCandidateList
from app.agent.tools.base import ToolResult, append_research, complete_json, pairs
from app.repositories import create_research_items
from app.services.llm import LLMService
from app.services.x_service import XService

logger = logging.getLogger(__name__)

# discovery 要比单主题研究多取一些：它的输入是一个宽泛的目标，
# 而不是一个已经收敛的题目。取 10 条会让候选选题全都长在同一个话题上。
DISCOVERY_RESULTS = 25

# 候选上限。工作单要求"至少产生 5 个候选"，所以下限靠 prompt 约束，
# 这里只做上限保护（防止模型一次吐几十条把后续 prompt 撑爆）。
MAX_TOPICS = 12
MIN_TOPICS = 5


def make_discover_tool(x_service: XService, llm_service: LLMService, session_factory):
    async def discover(state, decision) -> ToolResult:
        run_id = state["run_id"]
        goal = state.get("goal") or state.get("topic") or ""

        # 检索词优先取 Planner 给的 arguments.query：把"今天帮我找一个值得讨论的
        # AI 话题"这种自然语言目标直接丢给 X 搜索效果很差，而 Planner 有能力
        # 把它收敛成检索词。target 与 goal 是逐级降级的兜底。
        query = str(decision.arguments.get("query") or decision.target or goal)

        logger.info("agent discover started (run_id=%s, query=%r)", run_id, query)
        results = await x_service.research(query, max_results=DISCOVERY_RESULTS)
        async with session_factory() as session:
            item_ids = await create_research_items(session, run_id, results)

        delta = append_research(state, results, item_ids)
        prompt = render(
            load_prompt("discover"),
            GOAL=goal,
            MIN_TOPICS=MIN_TOPICS,
            MAX_TOPICS=MAX_TOPICS,
            RESEARCH=format_sources(pairs(item_ids, results)),
        )
        parsed: TopicCandidateList = await complete_json(
            llm_service, prompt, TopicCandidateList
        )
        topics = parsed.topics[:MAX_TOPICS]
        delta["candidate_topics"] = topics
        delta["research_count"] = counter(state, "research_count") + 1

        names = "；".join(t.topic for t in topics[:5]) or "无"
        logger.info("agent discover completed (run_id=%s, topics=%d)", run_id, len(topics))
        return ToolResult(
            delta=delta,
            summary=f"检索「{query}」取回 {len(results)} 条材料，发现 {len(topics)} 个候选选题：{names}",
        )

    return discover
