"""交给 X 的检索词：哪些会让整次运行直接失败。

真机 smoke 实测到的一个**硬失败**：候选选题的标题是一条 t.co 链接（材料正文
就是链接时标题就长那样）→ `research_topic` 拿标题去检索 →
`query=https%3A%2F%2Ft.co%2FRMhWA6lmX9` → X 返回 400
（`no viable alternative at character '/'`）→ 异常从工具里抛出去，Run 判失败。

这些测试钉住两件事：什么样的词不能用，以及不能用时**换成什么、怎么说明**。
第二件同样重要——"我们搜的不是 summary 里那个词"必须能看出来。
"""

import pytest_asyncio

from app.agent import search_query
from app.agent.schemas import AgentDecision
from app.agent.tools.research import make_research_tool
from app.agent.trends import LINK_ONLY_TITLE
from app.repositories import create_agent_run
from app.services.x_service import XService
from fakes import SAMPLE_X_RESPONSE, FakeXApiClient
from support import FIXED_NOW

# --------------------------------------------------------------------------
# usable
# --------------------------------------------------------------------------


def test_plain_keywords_are_usable():
    assert search_query.usable("AI Agent 智能体 讨论")
    assert search_query.usable("MCP")


def test_a_url_is_not_usable():
    """URL 含 `/` 与 `:`，X 的查询解析器两个都不接受。"""
    assert not search_query.usable("https://t.co/RMhWA6lmX9")
    assert not search_query.usable("http://example.com/a/b")


def test_the_operators_of_xs_query_language_are_not_usable():
    """括号在 X 的查询里是分组运算符，不是普通字符。"""
    assert not search_query.usable("(AI OR Agent) 讨论")
    assert not search_query.usable("has:links")
    assert not search_query.usable("MCP/Agent")


def test_blank_is_not_usable():
    """空 query 同样会被 X 判成参数非法——"没东西可搜"不是"能搜"。"""
    assert not search_query.usable("")
    assert not search_query.usable("   \n\t ")


def test_full_width_punctuation_is_fine():
    """中文全角括号只是普通字符。把它们也拦掉，等于把中文目标全判成不可搜。"""
    assert search_query.usable("研究一下 MCP（Model Context Protocol）的采用情况。")


# --------------------------------------------------------------------------
# choose
# --------------------------------------------------------------------------


def test_choose_takes_the_first_usable_candidate():
    query, note = search_query.choose([("题目", "MCP 采用"), ("目标", "别的")])
    assert (query, note) == ("MCP 采用", "题目")


def test_choose_skips_a_candidate_that_cannot_be_sent():
    query, note = search_query.choose(
        [("题目", "https://t.co/RMhWA6lmX9"), ("目标", "MCP 采用情况")]
    )
    assert query == "MCP 采用情况"
    assert note == "目标"


def test_choose_falls_back_to_a_degraded_copy_when_nothing_is_usable():
    """全都不能用时把非法字符换成空格，并且**说明里必须写明降级过**。

    说明是调用方写 summary 用的。如果这里悄悄降级、说明仍写"按题目检索"，
    界面上就只剩一个和材料对不上的题目，没人看得出中间换过词。
    """
    query, note = search_query.choose([("题目", "MCP/Agent 现状"), ("目标", "")])
    assert query == "MCP Agent 现状"
    assert "替换" in note and "题目" in note


def test_choose_does_not_invent_keywords_for_a_url():
    """降级只拿掉非法字符，不生成新词。

    一条 URL 降级后是一串碎片——难看，但它是**从原文来的**。
    编几个关键词看着会体面得多，那才是这一层绝不能做的事。
    """
    query, _ = search_query.choose([("题目", "https://t.co/RMhWA6lmX9")])
    assert query == "https t.co RMhWA6lmX9"
    assert "MCP" not in query


def test_choose_reports_when_there_is_nothing_to_search():
    query, note = search_query.choose([("题目", ""), ("目标", "  ")])
    assert query == ""
    assert "没有可用" in note


# --------------------------------------------------------------------------
# 工具层：一个 URL 题目不该让整次运行失败
# --------------------------------------------------------------------------


GOAL = "研究一下 MCP（Model Context Protocol）在企业级 AI Agent 集成里的采用情况。"


@pytest_asyncio.fixture
async def run_id(session_factory) -> str:
    """一条真实的 agent_run 行（`create_research_items` 的外键指着它）。"""
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=GOAL)
    return str(run.id)


def research_tool(session_factory):
    """返回 (工具, 假客户端)，方便断言**实际发给 X 的那个 query**。"""
    client = FakeXApiClient()
    return make_research_tool(XService(client, clock=lambda: FIXED_NOW), session_factory), client


async def test_research_searches_something_usable_when_the_topic_is_a_url(session_factory, run_id):
    """选定题目是一条链接时，用能搜的词去搜，而不是把 URL 发给 X。

    这是真机上的那条路径（smoke 3：题目 `https://t.co/RMhWA6lmX9` → 400 → 运行失败）。
    断言的是**实际发给 X 的那个 query**，而不是 state 里记的题目——两者在这里
    本来就是有意不同的。
    """
    tool, client = research_tool(session_factory)
    state = {"run_id": run_id, "goal": GOAL, "selected_topic": "https://t.co/RMhWA6lmX9"}

    result = await tool(state, AgentDecision(action="research_topic", reason="研究选定的题目"))

    assert [s["query"] for s in client.searches] == [GOAL]
    assert "https://" not in client.searches[0]["query"]
    # 换了词就要写在 summary 里，否则界面上只剩一个与材料对不上的题目。
    assert "按「" in result.summary
    assert len(result.delta["research_item_ids"]) == len(SAMPLE_X_RESPONSE["data"])


async def test_research_skips_the_placeholder_title_of_a_link_only_material(session_factory, run_id):
    """占位标题本身也不能当检索词。

    它是 discover 给"正文只有链接的材料"发的说明（`（材料只有链接，…）`），
    拿去检索只会取回一批准噪声——而它看起来完全能用，所以必须显式识别。
    """
    tool, client = research_tool(session_factory)
    state = {"run_id": run_id, "goal": GOAL, "selected_topic": LINK_ONLY_TITLE}

    await tool(state, AgentDecision(action="research_topic", reason="研究选定的题目"))

    assert [s["query"] for s in client.searches] == [GOAL]


async def test_research_still_searches_the_topic_when_it_is_searchable(session_factory, run_id):
    """能搜的题目照旧用题目本身——这条规则不该改变正常路径的检索词。"""
    tool, client = research_tool(session_factory)
    state = {"run_id": run_id, "goal": GOAL, "selected_topic": "英伟达发布开放智能体安全平台"}

    result = await tool(state, AgentDecision(action="research_topic", reason="研究选定的题目"))

    assert [s["query"] for s in client.searches] == ["英伟达发布开放智能体安全平台"]
    assert "按「" not in result.summary
