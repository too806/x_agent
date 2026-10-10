"""topic 与 goal 都是**这一次运行**的输入，不是全局状态。

实际用法是"今天发 AI、明天发美股、后天发机器人"，甚至直接指定一个具体事件——
这些全都是不同的 Run。本文件钉住的正是这件事：任何一个 Run 的题目/目标都不会
影响另一个 Run。

结构上由三样东西保证，逐条验：

1. `agent_run` 一行一次运行，子表（`research_item` / `draft` / `agent_decision`）
   全部挂 `agent_run_id` 外键；
2. LangGraph 的 `thread_id` 恒等于 `run_id`（`app/main.py::_thread_config`），
   checkpoint 因此天然按 Run 分开；
3. `topic` / `goal` 只活在这一次调用的 state 与那一行记录里——全仓库没有任何
   模块级的题目状态（`config/` 只有 agent / persona / style 三个文件，都不是题目）。

**为什么不用 `StubGraph`**：`tests/fakes.py::StubGraph` 对**每个** thread_id 返回
同一份 state（`test_learning_api.py` 的文档里也记了这件事），用它写出来的"隔离"
断言恒为真——那正是本文件要证伪的东西。所以这里一律走真实 checkpointer。

题目链与目标链各验一遍（两者写的是同一个 `agent_run.topic` 列，但来源完全不同），
再验两者混在同一个库里时互不越界。
"""

from sqlalchemy import select

from app.models import Draft, ResearchItem
from app.repositories import create_agent_run, get_agent_run
from app.services.x_service import XService
from fakes import FakeXApiClient
from support import FIXED_NOW, runtime_with_checkpointer
from test_agent_loop import HAPPY_PATH, config_for, drive, happy_llm

# 两个**领域完全不同**的题目。刻意不让它们共享任何词——共享词会让"互相污染"
# 在字符串断言里看不出来。
TOPIC_A = "英伟达为什么今天突然大涨"
TOPIC_B = "纳斯达克最近为什么大跌"

# 两条目标必须**各自都能被满足**，所以要带上假 X 服务真会返回的那个词：
# `SAMPLE_X_RESPONSE` 是两条讲 AI coding agent 的英文帖子。目标里的词一个都
# 不在材料里出现时，相关性闸门（`app/agent/relevance.py`）会**正确地**标记这批
# 候选与目标无关、拒绝选题，这条运行于是以"没有产出"收场——那是另一条测试要
# 验的路径（安全结束），不是这里要验的隔离性。
GOAL_AI = "今天关注 AI，找一个最近最值得讨论的话题"
GOAL_STOCK = "今天关注美股 AI 板块，找一个最近最值得讨论的话题"

#: 脚本里 `select_topic` 选中的那条候选的标题（见 `fakes.topics_payload`）。
#: 两条 goal 运行共用同一份脚本，所以选出来的题目**本来就该一样**——题目是脚本
#: 决定的，不是"上一条 Run 传给下一条的"。隔离要看的是别的东西（见下面的断言）。
SCRIPTED_TOPIC = "候选选题 1"


def _x_service() -> tuple[XService, FakeXApiClient]:
    """与 `support.agent_x_service()` 同样的构造，但把假客户端留在手上。

    `searches` 记着每次检索**真正发出去的 query**。"每条 Run 用自己那一题去搜"
    只能从这里看——库里只留下检索的**结果**，看不出搜的是什么。
    """
    client = FakeXApiClient()
    return XService(client, clock=lambda: FIXED_NOW), client


async def _topic_run(runtime, session_factory, topic: str) -> tuple[str, dict]:
    """V1 题目链跑一轮（人已经知道写什么）。"""
    async with session_factory() as session:
        run = await create_agent_run(session, topic)
        run_id = str(run.id)
    out = await runtime.graph.ainvoke(
        {"run_id": run_id, "topic": topic}, config=config_for(run_id)
    )
    return run_id, out


async def _draft_view(session_factory, run_id: str) -> list[tuple[str, int, str]]:
    """这条 Run 的草稿在库里的**值**（不是 ORM 对象）。

    比 ORM 对象相等是不可靠的：SQLAlchemy 的声明式模型没有自定义 `__eq__`，
    两个 session 里取出来的同一行是**不同的对象**，`==` 永远为假——那样写出来的
    "A 没被改动"断言恒为真，等于没测。
    """
    async with session_factory() as session:
        rows = (
            await session.execute(select(Draft).where(Draft.agent_run_id == run_id))
        ).scalars().all()
    return sorted((str(row.id), row.version, row.content or "") for row in rows)


async def _item_ids(session_factory, run_id: str) -> set[str]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ResearchItem.id).where(ResearchItem.agent_run_id == run_id)
            )
        ).scalars().all()
    return {str(item_id) for item_id in rows}


# ---------------------------------------------------------------------------
# Test 1 + Test 4：两条题目链，而且后跑的那条不改动先跑的那条
# ---------------------------------------------------------------------------


async def test_two_topic_runs_are_isolated(session_factory):
    x, client = _x_service()
    async with runtime_with_checkpointer(session_factory, x_service=x) as runtime:
        run_a, out_a = await _topic_run(runtime, session_factory, TOPIC_A)

        # 先给 A 拍一张快照，B 跑完之后再比一次
        state_a_before = (await runtime.graph.aget_state(config_for(run_a))).values
        items_a = await _item_ids(session_factory, run_a)
        drafts_a = await _draft_view(session_factory, run_a)

        run_b, out_b = await _topic_run(runtime, session_factory, TOPIC_B)

        # A 的 checkpoint 与落库产物**一个字节都没变**
        state_a_after = (await runtime.graph.aget_state(config_for(run_a))).values
        assert state_a_after == state_a_before
        assert await _item_ids(session_factory, run_a) == items_a
        assert await _draft_view(session_factory, run_a) == drafts_a

        # 两条 Run 的 checkpoint 是两份，不是同一份被覆盖
        state_b = (await runtime.graph.aget_state(config_for(run_b))).values
        assert state_b["topic"] == TOPIC_B
        assert state_a_after["topic"] == TOPIC_A

    # **检索词**各是各的题目：B 没有沿用 A 的题
    assert [s["query"] for s in client.searches] == [TOPIC_A, TOPIC_B]

    # 材料没有一条越界
    items_b = await _item_ids(session_factory, run_b)
    assert items_a & items_b == set()

    # 库里那一列也是各自的；题目链不带 goal（两条链路不互相顶替字段）
    async with session_factory() as session:
        row_a = await get_agent_run(session, run_a)
        row_b = await get_agent_run(session, run_b)
    assert (row_a.topic, row_a.goal) == (TOPIC_A, None)
    assert (row_b.topic, row_b.goal) == (TOPIC_B, None)
    assert out_a["topic"] == TOPIC_A and out_b["topic"] == TOPIC_B


# ---------------------------------------------------------------------------
# Test 2：两条目标链——planner 输入、检索词、产物都各归各的
# ---------------------------------------------------------------------------


async def test_two_goal_runs_get_their_own_goal_and_own_query(session_factory):
    x, client = _x_service()
    # 一个 runtime、一个脚本模型，连着跑两条 Run：`decisions` 是**逐次**消费的，
    # 所以把同一套路径接两遍。两条 Run 共用同一个进程、同一个 X 客户端、同一个库，
    # **只有 goal 不一样**——任何跨 Run 的残留都躲不掉。
    # 分开两个 runtime 也能过，但那样验的是"两个进程各自干净"，弱得多。
    llm = happy_llm()
    llm.decisions = list(HAPPY_PATH) * 2

    async with runtime_with_checkpointer(session_factory, x_service=x, llm_service=llm) as runtime:
        run_ai, _ = await drive(runtime, session_factory, llm, goal=GOAL_AI)
        searches_ai = [s["query"] for s in client.searches]
        items_ai = await _item_ids(session_factory, run_ai)
        drafts_ai = await _draft_view(session_factory, run_ai)

        run_stock, _ = await drive(runtime, session_factory, llm, goal=GOAL_STOCK)
        searches_stock = [s["query"] for s in client.searches[len(searches_ai):]]

    # 1) Planner 看到的 GOAL 是本条 Run 的目标，另一条的**一次都没出现过**
    planner = [c["prompt"] for c in llm.calls if c["kind"] == "planner"]
    for_ai = [prompt for prompt in planner if GOAL_AI in prompt]
    for_stock = [prompt for prompt in planner if GOAL_STOCK in prompt]
    assert len(for_ai) == len(HAPPY_PATH)  # 否则"没有串台"可能只是因为压根没跑到
    assert len(for_stock) == len(HAPPY_PATH)
    # 每一条 prompt 只带**一个** goal：不存在"两个目标都进了同一次决策"这种半串台
    assert len(for_ai) + len(for_stock) == len(planner)

    # 2) 检索词也是各自的。脚本没给 `arguments.query`，于是 discover 降级到
    #    **本条 Run 的目标文本**（app/agent/tools/discover.py 的 choose 链）
    assert searches_ai[0] == GOAL_AI
    assert searches_stock[0] == GOAL_STOCK
    assert GOAL_STOCK not in searches_ai
    assert GOAL_AI not in searches_stock

    # 3) 产物各归各的：A 在 B 跑完之后没被改动，材料也没有一条越界
    assert await _item_ids(session_factory, run_ai) == items_ai
    assert await _draft_view(session_factory, run_ai) == drafts_ai
    items_stock = await _item_ids(session_factory, run_stock)
    assert items_ai & items_stock == set()

    # 4) 两条 Run 各自记下了自己的目标
    async with session_factory() as session:
        row_ai = await get_agent_run(session, run_ai)
        row_stock = await get_agent_run(session, run_stock)
    assert row_ai.goal == GOAL_AI
    assert row_stock.goal == GOAL_STOCK
    # 题目由 Agent 自己选（脚本里是 t1），两条运行选到同一个标题是**脚本**的
    # 结果，不是"上一条传给下一条"——它来自各自 checkpoint 里的 selected_topic。
    assert row_ai.topic == SCRIPTED_TOPIC
    assert row_stock.topic == SCRIPTED_TOPIC


# ---------------------------------------------------------------------------
# Test 3：两条链路的语义不互相顶替
# ---------------------------------------------------------------------------


async def test_a_topic_run_and_a_goal_run_do_not_mix(session_factory):
    """同一个库里一条题目链 + 一条目标链，各写各的列、各存各的材料。

    - 题目链：`topic` = 人给的那句话，`goal` 为 NULL；
    - 目标链：`goal` = 人给的任务，`topic` = **Agent 自己选出来的题**。

    后者绝不能变成人给的那句话（那就成了把 goal 抄进 topic），也不能变成另一条
    Run 的题目。
    """
    x, _ = _x_service()

    # 两条链路要用两种假 LLM（Agent 要脚本化决策，题目链要 V1 的成稿 JSON），
    # 所以这里必须两个 runtime。这不削弱结论：要验的是**库**和**列**，而库是
    # 同一个 `session_factory`；两个 runtime 共用的还有同一个 X 客户端。
    llm = happy_llm()
    async with runtime_with_checkpointer(session_factory, x_service=x, llm_service=llm) as runtime:
        goal_run, out_goal = await drive(runtime, session_factory, llm, goal=GOAL_AI)
        items_goal = await _item_ids(session_factory, goal_run)

    async with runtime_with_checkpointer(session_factory, x_service=x) as runtime:
        topic_run, out_topic = await _topic_run(runtime, session_factory, TOPIC_B)
        items_topic = await _item_ids(session_factory, topic_run)

    async with session_factory() as session:
        goal_row = await get_agent_run(session, goal_run)
        topic_row = await get_agent_run(session, topic_run)

    assert (topic_row.topic, topic_row.goal) == (TOPIC_B, None)
    assert goal_row.goal == GOAL_AI
    # Agent 自己选的题落进 topic 列，且不是人给的任何一句话
    assert goal_row.topic == SCRIPTED_TOPIC
    assert goal_row.topic not in (GOAL_AI, TOPIC_B)

    # Agent 模式**不往 state 里写 topic**：题目在 `selected_topic` 里，最后落库。
    # 这一条同时是 app/agent/state.py 里 topic 字段说明的被测版本——那里以前写着
    # "Agent 运行时由 finalize 之后的镜像写入"，与事实不符。
    assert "topic" not in out_goal
    assert out_goal["selected_topic"] == SCRIPTED_TOPIC
    # 题目链反过来也不带 goal
    assert out_topic["topic"] == TOPIC_B

    assert items_goal and items_topic
    assert items_goal & items_topic == set()
