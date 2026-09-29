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

import pytest_asyncio

from app.agent.schemas import AgentDecision
from app.agent.tools.discover import make_discover_tool
from app.repositories import create_agent_run
from fakes import SAMPLE_X_RESPONSE, ScriptedLLMService
from support import agent_x_service

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


def clusters(sources_per_topic: list[list[str]]) -> str:
    """模型返回的分组结果。`sources_per_topic` 是每个簇声称引用的编号。"""
    return json.dumps(
        {
            "topics": [
                {
                    "title": f"候选选题 {index}",
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
    return make_discover_tool(
        agent_x_service(), ScriptedLLMService(responses={"discover": payload}), session_factory
    )


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
