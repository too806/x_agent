"""discover 工具端到端：**模型说了什么 → 实际变成什么候选**。

这两个缺陷都是真机 smoke run 第一次跑才暴露出来的，而离线套件当时全绿——
因为它们各自活在一个"看起来永远不会走到的分支"里：

1. **材料 id 抄错**。prompt 里印的是 36 位 UUID，模型一次要在 5～12 个簇里各抄
   若干条。真机结果：7 个簇全部核验不上（`kept=0, dropped=7`），整轮发现退化成
   "一条帖子一个候选"。修法是给材料发短编号（`m1`、`m2`…），翻回真 id 的事由
   工具做（见 `app/agent/tools/discover.py::_material_aliases`）。
2. **兜底路径本身就是坏的**。上面那次退化触发了兜底，而兜底的调用点多传了一个
   参数，直接 `TypeError`——一次本该"降级但继续"的运行变成整次失败。
   没有任何测试走过那一支，所以它一直绿着。

这两个测试因此都刻意走**完整工具调用**（真打分、真核验、真落库），而不是单独
测那几个辅助函数：两个缺陷都不在辅助函数里，而在它们被调用的地方。

   pytest -q tests/test_agent_discover.py
"""

import json
from datetime import timedelta

import pytest_asyncio

from app.agent.schemas import AgentDecision, TopicQualification
from app.agent.tools.discover import make_discover_tool, make_select_topic_tool
from app.agent.trends import LINK_ONLY_TITLE, TREND_WINDOW_HOURS
from app.repositories import create_agent_run
from app.services.x_service import XService
from fakes import SAMPLE_X_RESPONSE, FakeXApiClient, ScriptedLLMService
from support import FIXED_NOW

GOAL = "今天帮我找一个最近值得在中文科技 X 圈讨论的 AI Agent 话题。"


@pytest_asyncio.fixture
async def run_id(session_factory) -> str:
    """一条真实的 agent_run 行。

    `create_research_items` 会把材料挂到 `agent_run_id` 上（外键），所以
    "假装有个 run_id"在这里是行不通的——那正是这个工具与数据库真实的耦合点。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=GOAL)
    return str(run.id)


def clusters(sources_per_topic: list[list[str]], titles: list[str] | None = None) -> str:
    """模型返回的分组结果。`sources_per_topic` 是每个簇声称引用的编号。

    `titles` 用于那些**标题本身就是要测的东西**的场景（例如模型把一条链接
    当成了标题）。
    """
    return json.dumps(
        {
            "topics": [
                {
                    "title": (titles[index - 1] if titles else f"候选选题 {index}"),
                    "description": "材料在讲同一件事",
                    "keywords": ["关键词"],
                    "why_now": "刚刚发生",
                    "unknowns": ["还有一处没查到"],
                    "source_ids": sources,
                }
                for index, sources in enumerate(sources_per_topic, start=1)
            ]
        },
        ensure_ascii=False,
    )


def discover_state(run_id: str) -> dict:
    return {"run_id": run_id, "goal": GOAL}


def decide(**arguments) -> AgentDecision:
    return AgentDecision(
        action="discover_topics", reason="先找出一批候选", arguments=arguments
    )


def _tool(session_factory, payload: str):
    return _tool_with_client(session_factory, payload)[0]


def _tool_with_client(session_factory, payload: str, raw=None):
    """返回 (工具, 假客户端)：有些断言要看**实际发给 X 的那个 query**。"""
    client = FakeXApiClient(raw=raw)
    tool = make_discover_tool(
        XService(client, clock=lambda: FIXED_NOW),
        ScriptedLLMService(responses={"discover": payload}),
        session_factory,
    )
    return tool, client


# --------------------------------------------------------------------------
# 短编号 → 真 id
# --------------------------------------------------------------------------


async def test_the_llm_can_cite_short_handles_and_they_become_real_ids(session_factory, run_id):
    """模型照抄 prompt 里的 `m1`/`m2`，落进 state 的必须是真 id。

    候选上的 `source_post_ids` 与 `EvidenceFact.source_ids` 是**同一个 id 空间**
    （都是 `research_item.id`）。这里留一个 `m` 开头的值，追溯链就在第二步断了，
    而断链在下游只表现为"证据引不到材料"，看不出是这一行的问题。
    """
    tool = _tool(session_factory, clusters([["m1", "m2"], ["m1"]]))

    result = await tool(discover_state(run_id), decide(query="AI Agent 智能体"))

    topics = result.delta["candidate_topics"]
    assert len(topics) == 2
    for topic in topics:
        assert topic.source_post_ids, "候选必须带着出处"
        for source in topic.source_post_ids:
            assert not source.startswith("m")
            assert len(source) == 36, f"应当是 research_item.id，实际 {source!r}"
    # 两簇的引用都真实存在，来源质量不该被扣：分组质量与引用准确性分开记。
    assert all(topic.source_quality_score == 1.0 for topic in topics)


async def test_the_materials_still_get_stored_as_their_real_ids(session_factory, run_id):
    """落库的 id 与候选引用的 id 必须**是同一批**。

    只把 prompt 里的编号换掉、忘了换候选上的引用，两组 id 就会各说各话：
    候选引用得通、库里查不到。这条直接比对两个来源。
    """
    tool = _tool(session_factory, clusters([["m1", "m2"]]))

    result = await tool(discover_state(run_id), decide(query="AI Agent 智能体"))

    stored = set(result.delta["research_item_ids"])
    cited = {s for topic in result.delta["candidate_topics"] for s in topic.source_post_ids}
    assert stored == set(s for s in cited)
    assert len(stored) == len(SAMPLE_X_RESPONSE["data"])


# --------------------------------------------------------------------------
# 引用核验不上时的处置
# --------------------------------------------------------------------------


async def test_a_made_up_handle_still_costs_source_quality(session_factory, run_id):
    """编一个不存在的编号，要照旧算"声称过但没核验上"。

    如果翻译时把认不出的编号**过滤掉**，"编了一个来源"就变成了"没有声称来源"——
    从分数上看，编造反而比诚实更划算（`source_quality` 的分母是声称数）。
    """
    tool = _tool(session_factory, clusters([["m1", "m999"], ["m2"]]))

    result = await tool(discover_state(run_id), decide(query="AI Agent 智能体"))

    mixed = next(t for t in result.delta["candidate_topics"] if len(t.source_post_ids) == 1)
    # 声称 2 条、只核验上 1 条。分母是声称数，所以是 0.5 而不是 1.0。
    assert mixed.source_quality_score == 0.5
    assert "m999" not in mixed.source_post_ids


async def test_clusters_citing_nothing_real_fall_back_instead_of_crashing(session_factory, run_id):
    """整组引用全核验不上时，退化成"一条帖子一个候选"，而不是报错。

    这就是真机上的那条路径（`kept=0, dropped=7`）。兜底存在的意义是"一次发现
    失败不该让整条 Run 连一个候选都没有"，所以它必须真的走得通；
    退化本身也要写进 summary——否则"退化了"与"最近确实没什么可讨论的"
    在结果上长得一模一样。
    """
    tool = _tool(session_factory, clusters([["zzz-1"], ["zzz-2"], ["zzz-3"]]))

    result = await tool(discover_state(run_id), decide(query="AI Agent 智能体"))

    topics = result.delta["candidate_topics"]
    assert len(topics) == len(SAMPLE_X_RESPONSE["data"]), "兜底应当一条帖子给一个候选"
    assert "退化" in result.summary
    # 退化出来的候选带着真实出处，只是结构上撑不起一个趋势（单一来源上限）。
    assert all(topic.source_post_ids for topic in topics)


# --------------------------------------------------------------------------
# 检索词与标题：一条链接不该沿着链路一路往下走
# --------------------------------------------------------------------------

#: 检索回来的材料里有一类帖子正文就是链接（分享型）。真机 smoke 里，
#: 整轮发现只回来这样一条材料，于是候选标题、选定题目、检索词全都是那条链接。
LINK_ONLY_X_RESPONSE = {
    "data": [
        {
            "id": "2001",
            "text": "https://t.co/RMhWA6lmX9",
            "author_id": "u9",
            "created_at": "2026-09-18T10:00:00.000Z",
            "public_metrics": {
                "retweet_count": 0,
                "reply_count": 0,
                "like_count": 0,
                "quote_count": 0,
            },
        },
        dict(SAMPLE_X_RESPONSE["data"][0]),
    ],
    "includes": {"users": [{"id": "u9", "name": "Cara", "username": "cara_ai"}]},
    "meta": {"result_count": 2, "newest_id": "2001", "oldest_id": "1001"},
}


async def test_a_title_that_is_only_a_link_is_replaced_by_a_label(session_factory, run_id):
    """候选标题不能是一条网址。

    材料里有一类帖子正文就是链接（分享型），模型给这种材料分组时标题就长成链接。
    真机 smoke 里出现过 `https://t.co/RMhWA6lmX9` 这样的候选，而且因为没有别的
    达标候选，`select_topic` 按"分数最高的"选中了它，接着 `research_topic`
    拿它去检索 → X 400 → 整次运行失败（见 app/agent/search_query.py）。

    替换文本只陈述事实（这条材料只有链接），不编造内容。
    """
    payload = clusters([["m1", "m2"], ["m1"]], titles=["https://t.co/RMhWA6lmX9", "候选选题 2"])
    tool = _tool(session_factory, payload)

    result = await tool(discover_state(run_id), decide(query="AI Agent 智能体"))

    titles = [topic.title for topic in result.delta["candidate_topics"]]
    assert LINK_ONLY_TITLE in titles
    assert not [t for t in titles if t.startswith("http")], "标题里不该留下网址"


async def test_the_fallback_never_uses_a_raw_link_as_a_title(session_factory, run_id):
    """兜底路径也一样：正文只有链接的材料，不能拿链接当标题。

    这正是真机 smoke 3 走过的路径——检索只回来一条材料，而那条材料的正文
    就是链接，模型给不出可用分组，于是退化成"一条帖子一个候选"，
    标题 = 正文 = URL。换掉标题**不改打分**：单一来源本来就该被压到 40 分
    以下、判成 weak_support。

    如果换标题顺带把这类材料从打分里摘出去，"链接材料"就会从"低分但看得见"
    变成"不存在"，而后者从报告上看不出发生过什么。
    """
    tool = _tool_with_client(
        session_factory, clusters([["zzz-1"], ["zzz-2"]]), raw=LINK_ONLY_X_RESPONSE
    )[0]

    result = await tool(discover_state(run_id), decide(query="AI Agent 智能体"))

    by_title = {topic.title: topic for topic in result.delta["candidate_topics"]}
    assert LINK_ONLY_TITLE in by_title
    link_topic = by_title[LINK_ONLY_TITLE]
    assert link_topic.source_post_ids, "材料还在候选上挂着"
    assert link_topic.trend_score <= 40.0
    # 同批材料里正文正常的那些照旧用正文当标题。
    assert "AI coding agents are transforming how teams ship software." in by_title


async def test_discover_never_sends_a_query_x_cannot_parse(session_factory, run_id):
    """Planner 给的检索词里带 `/` 或链接时，换一个能用的，而不是撞 400。

    断言的是**实际发给 X 的那个 query**：state 里记的 goal 仍然是原文，
    两者不同是有意的。
    """
    tool, client = _tool_with_client(
        session_factory, clusters([["m1", "m2"]])
    )

    await tool(discover_state(run_id), decide(query="https://t.co/RMhWA6lmX9"))

    sent = client.searches[0]["query"]
    assert "https" not in sent
    assert sent == GOAL  # 检索词一路降级到目标文本


async def test_discover_declares_the_scan_window_it_used(session_factory, run_id):
    """72 小时是一个**执行事实**，不是一个写在文档里的常数。

    X 的服务端过滤靠 start_time / end_time 做（`x_api` 刻意不在本地丢，
    否则窗口外的帖子会占满 max_results）。少传一个等于让 X 用他自己的默认
    范围回答我们——回来的材料看着正常，只是不是被问到的那一段。
    """
    tool, client = _tool_with_client(session_factory, clusters([["m1", "m2"]]))

    await tool(discover_state(run_id), decide(query="AI Agent 智能体"))

    sent = client.searches[0]
    assert sent["start_time"] == FIXED_NOW - timedelta(hours=TREND_WINDOW_HOURS)
    assert sent["end_time"] == FIXED_NOW


# --------------------------------------------------------------------------
# 目标相关性：候选与用户目标对不上时必须**标记出来**（工作单第二、四节）
# --------------------------------------------------------------------------


HONG_KONG_GOAL = "今天想关注香港"


async def test_materials_that_mention_the_goal_mark_the_candidate_relevant(
    session_factory, run_id
):
    """GOAL 里的「AI Agent」在材料里出现过 → 判相关，并写清命中了哪个词。

    两个簇各引两条材料，是为了**不触发按帖子分组的兜底**（`FALLBACK_MIN_TOPICS`
    ＝2）：兜底出来的候选是"一条帖子一个候选"，那样测到的相关性判定对象
    就不是模型给的分组了。
    """
    tool = _tool(session_factory, clusters([["m1", "m2"], ["m1", "m2"]]))

    result = await tool(discover_state(run_id), decide(query="AI Agent"))

    topics = result.delta["candidate_topics"]
    assert [t.goal_relevance for t in topics] == [True, True]
    # ASCII 词在 `relevance.goal_terms` 里统一小写，所以比对也不分大小写。
    assert "agent" in topics[0].goal_relevance_note.lower()


async def test_candidates_whose_materials_miss_the_goal_are_marked_and_refused(
    session_factory, run_id
):
    """Run 20ab360c 的复现（工作单第二、三、四节）。

    目标是「今天想关注香港」，而材料是两条讲 AI coding agent 的英文帖子——
    目标里的话题词一个都没出现。此时的判定必须是 **False**（不是"未判定"），
    而且这件事必须**说出来**：summary 里要写清淘汰了几个、为什么。

    然后 `select_topic` 必须**拒绝**。原先这里回退到"候选里分最高的那个"，
    于是用户拿到一篇讲 FF 发布会的分析——trace 里那条回退记录看起来完全正常。

    这个候选本身是**达标**的（两条材料、两位作者），所以拒绝它不是因为
    "材料不够"，而是因为它与目标无关——这两件事的处置完全相反。
    """
    tool = _tool(session_factory, clusters([["m1", "m2"], ["m1", "m2"]]))
    state = {**discover_state(run_id), "goal": HONG_KONG_GOAL}

    result = await tool(state, decide(query="香港"))

    topics = result.delta["candidate_topics"]
    assert len(topics) == 2
    assert [t.goal_relevance for t in topics] == [False, False]
    # 达标与否是另一回事：这两条候选本身有两条材料、两位作者。
    assert all(t.qualification is TopicQualification.QUALIFIED for t in topics)
    assert "香港" in topics[0].goal_relevance_note
    assert "淘汰" in result.summary
    assert "香港" in result.summary

    refusal = await make_select_topic_tool()(
        {**state, **result.delta}, decide(target=topics[0].topic_id)
    )
    assert refusal.delta == {}
    assert "没有可用的候选选题" in refusal.summary
    assert "换一个检索词" in refusal.summary
