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

from app.agent.budget import counter, is_selectable
from app.agent.prompts import format_sources, load_prompt, render
from app.agent.trends import is_link_only_title, scan_window
from app.agent.tools.base import (
    ToolResult,
    append_attempts,
    append_research,
    complete_json,
    pairs,
    plan_search,
)
from app.repositories import create_research_items
from app.services.llm import LLMService
from app.services.x_service import XService

logger = logging.getLogger(__name__)

RESEARCH_RESULTS = 10


def _choose_topic(state, decision) -> tuple[str, str]:
    """决定研究哪个题目，返回 (题目, 说明怎么选出来的)。

    优先级是刻意的，从"已经定下来的"到"临时猜的"：

    1. **`selected_topic` 已经有值** → 直接用它。这个值要么来自 `select_topic`
       （分数排序的结果），要么来自上一轮 research；两种情况下重新挑一次都等于
       把已经落在 state 上的决定再推翻一遍。
    2. Planner 的 target 只有在**确实是一个可被选中的候选**时才被采信（认标题或
       topic_id）。不采信一个不存在的题目名（可能是模型记错或改了措辞）：按它去
       搜索会得到一批与任何候选都对不上的材料，而证据阶段再拿这些材料去支撑观点，
       整条链就脱离了 topic 候选集。
    3. 回退到**可被选中的候选里分数最高**的那个，并在 trace 里写明发生了回退。
    4. 一个可用的候选都没有：用目标文本兜底。

    第 2、3 步从"候选之一 / 分最高的候选"收紧成"**可被选中的**候选"，是因为
    这个工具的末尾会把题目写进 `selected_topic`——也就是说**它本身就是一次选题**。
    Run 20ab360c 里即使 Planner 没调 select_topic，这条回退也会把那个与目标无关的
    候选定成整次运行的题目，然后一路研究、抽证据、写稿。闸门只装在 select_topic
    上的话，这条路径就绕过去了。

    没有可用候选时退回目标文本，而不是"分最高的那个候选"：目标文本至少是用户
    自己写的东西（这一轮的目的就是换一个更合适的检索词），而一个被判定为不达标
    或不相关的候选，是系统自己已经说过"不值得继续"的东西。
    """
    if state.get("selected_topic"):
        return str(state["selected_topic"]), "沿用已选定的题目"

    candidates = [c for c in (state.get("candidate_topics") or []) if is_selectable(c)]
    if decision.target:
        chosen = next(
            (c for c in candidates if c.title == decision.target or c.topic_id == decision.target),
            None,
        )
        if chosen is not None:
            return chosen.title, "按决策选定"

    if candidates:
        best = max(candidates, key=lambda c: c.trend_score)
        if decision.target:
            return best.title, f"决策给出的「{decision.target}」不是可用的候选，回退到最高分"
        return best.title, "决策未指定题目，取可选中候选里分数最高的"

    # 一个可用的候选都没有：用目标本身兜底，至少让这一轮取回的材料与目标相关。
    return str(state.get("goal") or state.get("topic") or ""), "无可用的候选选题，用目标文本兜底"


def _search_options(topic: str, state, decision) -> list[tuple[str, str]]:
    """这一轮检索可以用哪些词，按优先级排列。

    题目排第一：选定的题目是这一轮研究**语义上**该搜的东西。但它不一定**能**搜：
    候选选题里有一类标题本身就是一条链接（材料正文只有链接时，discover 会把它
    换成占位文本，而占位文本同样不能拿去检索），URL 则会被 X 的查询解析器判成
    非法参数、整次运行到此为止（见 `app/agent/search_query.py`）。

    所以后面跟两个兜底：Planner 给的检索词、目标文本。

    顺序在这里是唯一的规则来源——`plan_search()` 只负责取第一个能用的，
    并在连续空结果时把结果换成加宽后的词（见 `app/agent/query_broadening.py`）。
    """
    options: list[tuple[str, str]] = []
    if not is_link_only_title(topic):
        options.append(("题目", topic))
    options.append(("决策指定的检索词", str(decision.arguments.get("query") or "")))
    options.append(("目标", str(state.get("goal") or state.get("topic") or "")))
    return options


def make_research_tool(x_service: XService, session_factory):
    async def research_topic(state, decision) -> ToolResult:
        run_id = state["run_id"]
        topic, how = _choose_topic(state, decision)
        query, note = plan_search(state, _search_options(topic, state, decision))
        logger.info("agent research started (run_id=%s, topic=%r, query=%r)", run_id, topic, query)
        if query != topic:
            # 检索词与题目不一致必须留痕：材料看起来会与 UI 上显示的题目对不上，
            # 而"题目不能当检索词"是很具体的一个原因，值得说清楚。
            logger.warning(
                "agent research search query differs from the selected topic "
                "(run_id=%s, topic=%r, query=%r, why=%s)",
                run_id,
                topic,
                query,
                note,
            )

        # 与 discover 用**同一个**窗口：研究链路对"最近"只能有一个定义。
        # 不带窗口的检索会退化成 X 的默认 recent search（最近 7 天），
        # 窗口外的材料就一路混进打分——真机 smoke 实测到过最早早约 7 天的材料。
        observed_at = x_service.now()
        window_start, window_end = scan_window(observed_at)
        results = await x_service.research(
            query,
            max_results=RESEARCH_RESULTS,
            start_time=window_start,
            end_time=window_end,
            observed_at=observed_at,
        )
        async with session_factory() as session:
            item_ids = await create_research_items(session, run_id, results)

        delta = append_research(state, results, item_ids)
        delta.update(append_attempts(state, query, len(results)))
        delta["selected_topic"] = topic
        delta["research_count"] = counter(state, "research_count") + 1
        logger.info("agent research completed (run_id=%s, items=%d)", run_id, len(results))
        # 检索词与题目不一致时把它写进 summary：只写在日志里，界面上就只剩一个
        # 与材料对不上的题目。
        where = f"主题「{topic}」" if query == topic else f"主题「{topic}」，按「{query}」检索"
        return ToolResult(
            delta=delta,
            summary=f"{how}{where}，取回 {len(results)} 条材料",
        )

    return research_topic


def _deepen_options(state, decision) -> list[tuple[str, str]]:
    """深化研究的检索词候选，按优先级排列。

    优先级：Planner 明确给的 query → 证据包里的 unknowns → counterpoints → 原题。
    每条都可能含 X 不接受的字符（未知项常常是一句话，里面完全可能出现 `A/B`），
    所以交给 `plan_search()` 取第一个**能用**的，而不是取第一个非空的。
    """
    options: list[tuple[str, str]] = []
    if decision.arguments.get("query"):
        options.append(("按决策指定的检索词", str(decision.arguments["query"])))

    pack = state.get("evidence_pack")
    if pack is not None:
        if pack.unknowns:
            options.append(("针对证据里记下的未知项", pack.unknowns[0]))
        if pack.counterpoints:
            options.append(("针对证据里记下的反方说法", pack.counterpoints[0]))

    options.append(("没有已知空白，按原题再检索一轮", str(state.get("selected_topic") or "")))
    return options


def make_deepen_tool(x_service: XService, session_factory):
    async def deepen_research(state, decision) -> ToolResult:
        run_id = state["run_id"]
        query, how = plan_search(state, _deepen_options(state, decision))
        logger.info("agent deepen started (run_id=%s, query=%r)", run_id, query)

        # 深化检索也必须落在同一个窗口里：它取回的材料与首次研究**一起**进入
        # 证据与打分，两条路径的窗口不一致等于同一个话题有两套时间口径。
        observed_at = x_service.now()
        window_start, window_end = scan_window(observed_at)
        results = await x_service.research(
            query,
            max_results=RESEARCH_RESULTS,
            start_time=window_start,
            end_time=window_end,
            observed_at=observed_at,
        )
        async with session_factory() as session:
            item_ids = await create_research_items(session, run_id, results)

        delta = append_research(state, results, item_ids)
        delta.update(append_attempts(state, query, len(results)))
        delta["research_count"] = counter(state, "research_count") + 1
        logger.info("agent deepen completed (run_id=%s, items=%d)", run_id, len(results))
        return ToolResult(
            delta=delta,
            summary=f"{how}检索「{query}」，追加 {len(results)} 条材料",
        )

    return deepen_research
