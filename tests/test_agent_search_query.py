"""交给 X 的检索词：哪些会让整次运行直接失败。

真机 smoke 实测到的一个**硬失败**：候选选题的标题是一条 t.co 链接（材料正文
就是链接时标题就长那样）→ `research_topic` 拿标题去检索 →
`query=https%3A%2F%2Ft.co%2FRMhWA6lmX9` → X 返回 400
（`no viable alternative at character '/'`）→ 异常从工具里抛出去，Run 判失败。

这些测试钉住两件事：什么样的词不能用，以及不能用时**换成什么、怎么说明**。
第二件同样重要——"我们搜的不是 summary 里那个词"必须能看出来。
"""

from datetime import datetime, timedelta

import pytest_asyncio

from app.agent import search_query
from app.agent.schemas import AgentDecision
from app.agent.tools.research import make_deepen_tool, make_research_tool
from app.agent.trends import LINK_ONLY_TITLE, TREND_WINDOW_HOURS
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


def test_the_pipe_operator_is_not_usable():
    """`|` 是 X 查询语法里的 OR 运算符，单独出现就是语法错误。

    真机 smoke 4 就是这样整次失败的：`discover` 的兜底路径拿帖子正文当标题，
    正文里的并列写法（`A | B`）被当成检索词发出去，X 回
    `400 ... no viable alternative at character '|' (at position 20)`。
    `|` 不在黑名单里的那段时间，这类 400 是一条**真实发生过**的失败路径。
    """
    assert not search_query.usable("A | B")
    assert not search_query.usable("英伟达 | OpenAI 安全团队")


def test_a_pipe_is_degraded_to_a_space_not_dropped():
    """`|` 与其他非法字符同一种处置：换成空格，并在说明里写明降级过。

    刻意不为它加一条"遇到 `|` 就中止"的新分支——一个字符废掉整次运行，
    比降级一个词更糟（`A | B` → `A B` 至少搜的是同一批关键词）。
    """
    query, note = search_query.choose([("题目", "英伟达 | OpenAI 安全团队")])
    assert query == "英伟达 OpenAI 安全团队"
    assert "|" not in query
    assert "替换" in note


def test_a_wildcard_is_not_usable():
    """`*` 是通配符运算符，出现在一个词内部就是语法错误。

    5B.3 真机 smoke 4 实测到的失败路径：目标里带 Markdown 强调
    （`**双方吵起来**`），候选选题被判定不可检索，检索词兜底到目标原文，
    X 回 `400 ... Wildcard '*' character cannot appear in a term`。

    目标文本是人写的，人会在里面写 Markdown。这条路径不是"模型偶尔抽风"。
    """
    assert not search_query.usable("A* B")
    assert not search_query.usable("**双方吵起来**")
    assert not search_query.usable("帮我找一个最近的中文 AI Agent 话题：*重点*")


def test_a_wildcard_is_degraded_to_a_space():
    """与 `|` 同一种处置。降级后剩下的仍是原句里的词，搜的是同一件事。"""
    query, note = search_query.choose([("目标", "**双方吵起来**的 AI Agent 话题")])
    assert query == "双方吵起来 的 AI Agent 话题"
    assert "*" not in query
    assert "替换" in note


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


# --------------------------------------------------------------------------
# `A | B`：真机 smoke 4 的失败路径
# --------------------------------------------------------------------------


async def test_a_pipe_query_never_reaches_x(session_factory, run_id):
    """选定的题目含 `|` 时，发给 X 的必须换成能用的词。

    这是 smoke 4 那条路径的回归：题目里的 `|` 原样发出去 → X 回 400
    （`no viable alternative at character '|'`）→ 异常从工具里抛出去 → Run 判失败。
    断言的是**实际发给 X 的每一个 query**：state 里记的题目仍然是原文，
    两者不同是有意的（与上面那条 URL 题目同一套处置）。
    """
    tool, client = research_tool(session_factory)
    state = {
        "run_id": run_id,
        "goal": GOAL,
        "selected_topic": "英伟达 | OpenAI 安全团队",
    }

    await tool(state, AgentDecision(action="research_topic", reason="研究选定的题目"))

    assert client.searches, "该搜还是要搜——降级不等于中止"
    assert all("|" not in sent["query"] for sent in client.searches)
    assert client.searches[0]["query"] == GOAL


async def test_a_pipe_is_stripped_even_when_nothing_else_is_usable(session_factory, run_id):
    """没有别的候选可换时，降级后的词里也不能留下 `|`。"""
    tool, client = research_tool(session_factory)
    # 既没有 goal 也没有别的候选：`choose()` 只剩"把题目降级"这一条路
    state = {"run_id": run_id, "selected_topic": "英伟达 | OpenAI 安全团队"}

    result = await tool(state, AgentDecision(action="research_topic", reason="研究选定的题目"))

    assert all("|" not in sent["query"] for sent in client.searches)
    assert client.searches[0]["query"] == "英伟达 OpenAI 安全团队"
    # 换了词就要能从 summary 里看出来：只写题目的话，界面上就只剩一个
    # 与材料对不上的题目，没人看得出中间换过词。
    assert "英伟达 OpenAI 安全团队" in result.summary


# --------------------------------------------------------------------------
# 72 小时窗口：三条检索路径必须共用同一个定义
#
# 真机 smoke 实测到的分裂：`discover_topics` 带着窗口，`research_topic` 与
# `deepen_research` 不带，于是这两条退化成 X 的默认 recent search（最近 7 天），
# 窗口外的材料（最早早约 7 天）一路混进打分——"72 小时"只约束了链路的第一步。
# --------------------------------------------------------------------------


def deepen_tool(session_factory):
    client = FakeXApiClient()
    return make_deepen_tool(XService(client, clock=lambda: FIXED_NOW), session_factory), client


def assert_window_is_the_seventy_two_hours(sent: dict) -> None:
    assert sent["start_time"] == FIXED_NOW - timedelta(hours=TREND_WINDOW_HOURS)
    assert sent["end_time"] == FIXED_NOW


async def test_research_topic_carries_the_same_window_as_discovery(session_factory, run_id):
    tool, client = research_tool(session_factory)
    state = {"run_id": run_id, "goal": GOAL, "selected_topic": "英伟达发布开放智能体安全平台"}

    await tool(state, AgentDecision(action="research_topic", reason="研究选定的题目"))

    assert_window_is_the_seventy_two_hours(client.searches[0])


async def test_deepen_research_carries_the_same_window_as_discovery(session_factory, run_id):
    tool, client = deepen_tool(session_factory)
    state = {"run_id": run_id, "goal": GOAL, "selected_topic": "英伟达发布开放智能体安全平台"}

    await tool(state, AgentDecision(action="deepen_research", reason="证据里还有空白"))

    assert_window_is_the_seventy_two_hours(client.searches[0])


#: 两条帖子：一条 2 小时前（窗口内），一条 150 小时前（6.25 天前）。
#: `FIXED_NOW` 是 2026-09-18T12:00Z，所以后者是 2026-09-12T06:00Z。
#:
#: 150 小时这个数是**刻意挑的**：它在 72 小时窗口之外，却在 X recent search
#: 自身的 7 天覆盖范围之内。于是"窗口有没有真的传下去"决定了这条材料能不能回来，
#: 而两次请求的 query 完全相同——把它和窗口一起测，才分得清是哪一项在起作用。
WINDOWED_X_RESPONSE = {
    "data": [
        {
            "id": "3001",
            "text": "刚刚发生的讨论",
            "author_id": "u1",
            "created_at": "2026-09-18T10:00:00.000Z",
            "public_metrics": {"retweet_count": 1, "reply_count": 1, "like_count": 5,
                               "quote_count": 0},
        },
        {
            "id": "3002",
            "text": "六天前的旧讨论",
            "author_id": "u2",
            "created_at": "2026-09-12T06:00:00.000Z",
            "public_metrics": {"retweet_count": 1, "reply_count": 1, "like_count": 5,
                               "quote_count": 0},
        },
    ],
    "includes": {"users": [{"id": "u1", "name": "Ada", "username": "ada_dev"},
                           {"id": "u2", "name": "Bob", "username": "bob_codes"}]},
    "meta": {"result_count": 2, "newest_id": "3001", "oldest_id": "3002"},
}

#: X recent search 自带的覆盖范围（调用方不声明窗口时就是这个）。
RECENT_SEARCH_DAYS = 7


class WindowedFakeXApiClient(FakeXApiClient):
    """按收到的窗口做服务端过滤的假 X——**X 自己就是这么做的**。

    为什么不直接用 `FakeXApiClient`：它对任何窗口都回同一批帖子。那样一来
    "调用方没传窗口"这个缺陷在测试里完全看不出来——材料照样回来，只是来错了
    时间。把过滤行为补上之后，"窗口有没有真的传到 X"就成了一条能被断言的事实。
    """

    async def search_recent_tweets(self, query, max_results=10, *, start_time=None, end_time=None):
        raw = await super().search_recent_tweets(
            query, max_results, start_time=start_time, end_time=end_time
        )
        lower = start_time or FIXED_NOW - timedelta(days=RECENT_SEARCH_DAYS)
        upper = end_time or FIXED_NOW
        kept = [
            post
            for post in raw["data"]
            if lower <= datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")) <= upper
        ]
        return {**raw, "data": kept, "meta": {**raw["meta"], "result_count": len(kept)}}


def windowed_research_tool(session_factory):
    client = WindowedFakeXApiClient(raw=WINDOWED_X_RESPONSE)
    return make_research_tool(XService(client, clock=lambda: FIXED_NOW), session_factory), client


async def test_material_outside_the_window_cannot_enter_the_run(session_factory, run_id):
    """窗口外的材料进不来——把窗口去掉，这条测试立刻失败。

    这正是真机上发生过的事：`research_topic` 不带窗口时，X 按自己的默认范围
    （最近 7 天）过滤，150 小时前那条帖子照常回来、照常落库、照常参与打分。
    """
    tool, client = windowed_research_tool(session_factory)
    state = {"run_id": run_id, "goal": GOAL, "selected_topic": "英伟达发布开放智能体安全平台"}

    result = await tool(state, AgentDecision(action="research_topic", reason="研究选定的题目"))

    # 材料少了一条，原因必须是窗口，而不是 query 变了
    assert_window_is_the_seventy_two_hours(client.searches[0])
    returned = result.delta["research_results"]
    assert [r.content for r in returned] == ["刚刚发生的讨论"], "窗口外的材料不该进来"
    # 落库的那些也必须都在窗口内——判断用的是 `published_at`，不是入库时刻。
    assert all(
        r.published_at >= FIXED_NOW - timedelta(hours=TREND_WINDOW_HOURS) for r in returned
    )
