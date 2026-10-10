"""连续空结果时，检索词必须**变宽**，而不是换一组同样窄的词。

真机 run `f2c82f39` 的失败链：目标是"来一个脱离世俗的主题，言语简单些"，
Planner 连续四次给出 3–4 个中文词的检索词，四次全部取回 0 条，
整个 research 预算（4 次）被耗光，Run 以"未产出任何候选正文"收场。

    脱离世俗 出世 精神生活 讨论 → 0
    隐居 山居 极简生活 慢下来   → 0
    独居 慢生活 逃离内卷        → 0
    寺庙 禅修 年轻人            → 0

X 的 recent search 把空格当 AND，词越多召回越窄。真机实测（同一 token、
同一条 API、同一个 72 小时窗口）1 个词必然命中（池子里 16 个词全部回来 25 条），
而 2 个词仍有 3/8 组为 0、命中时也只回来 1–2 条——撑不起 `trends` 的 qualified
门槛（≥2 位作者、≥2 条来源）。

所以这里的断言**不写死具体中文词**：钉的是结构——词数只降不升、不重复、
不编造——而不是"应该换成'禅修'"。
"""

import pytest
import pytest_asyncio

from app.agent import query_broadening, search_query
from app.agent.nodes.finalize import AgentIncompleteError
from app.agent.schemas import AgentDecision, SearchAttempt
from app.agent.tools.base import plan_search
from app.agent.tools.discover import make_discover_tool
from app.repositories import create_agent_run
from app.services.x_service import XService
from fakes import FakeXApiClient, ScriptedLLMService, decision
from support import FIXED_NOW, runtime_with_checkpointer

# 真机失败 run 的原样输入。
FAILED_GOAL = "来一个脱离世俗的主题，言语简单些"
FAILED_QUERIES = (
    "脱离世俗 出世 精神生活 讨论",
    "隐居 山居 极简生活 慢下来",
    "独居 慢生活 逃离内卷",
    "寺庙 禅修 年轻人",
)

EMPTY_RESPONSE = {"data": [], "meta": {"result_count": 0}}


def empty_attempts(*queries: str) -> list[SearchAttempt]:
    return [SearchAttempt(query=q, result_count=0) for q in queries]


def discover_decision(**arguments) -> AgentDecision:
    """直接调工具时用的决策对象。

    与脚本用的 `decision()`（返回 dict，交给 planner 校验）不是一回事：
    这里要的是一个**已经构造好**的 `AgentDecision`。
    """
    return AgentDecision(
        action="discover_topics", reason="再找一轮材料", arguments=arguments
    )


# --------------------------------------------------------------------------
# 切词
# --------------------------------------------------------------------------


def test_terms_are_split_on_whitespace_and_punctuation():
    assert query_broadening.split_terms("脱离世俗 出世，精神生活/讨论") == [
        "脱离世俗",
        "出世",
        "精神生活",
        "讨论",
    ]


def test_markdown_and_query_operators_are_cut_out():
    """`*` 与 `|` 是 X 查询语法里的运算符，整条留着就是一次 400。

    切掉它们比留给 `search_query.usable()` 把整条检索词丢掉更有用：
    `**重点**` 应当变成一个能搜的词，而不是什么都不剩。
    """
    assert query_broadening.split_terms("**重点** A | B") == ["重点", "A", "B"]


def test_empty_text_has_no_terms():
    assert query_broadening.split_terms("") == []
    assert query_broadening.split_terms("   ") == []


# --------------------------------------------------------------------------
# Case A：一次空结果还不该加宽
# --------------------------------------------------------------------------


def test_no_attempts_means_no_broadening():
    assert (
        query_broadening.broadened_query([], current="寺庙 禅修 年轻人", goal=FAILED_GOAL)
        is None
    )


def test_one_empty_result_still_allows_a_same_complexity_retry():
    """第一次空结果更可能是"这组词碰巧不命中"，换一组等复杂度的词仍然合理。

    这是**刻意保留 Planner 自主性**的那一档：只有连着两次都空，才说明问题
    不在词选得准不准，而在约束太多。
    """
    attempts = empty_attempts(FAILED_QUERIES[0])
    assert (
        query_broadening.broadened_query(
            attempts, current=FAILED_QUERIES[1], goal=FAILED_GOAL
        )
        is None
    )


def test_a_hit_resets_the_counter():
    attempts = [
        SearchAttempt(query="a b c", result_count=0),
        SearchAttempt(query="d e", result_count=3),
        SearchAttempt(query="f g h", result_count=0),
    ]
    assert query_broadening.consecutive_empty(attempts) == 1
    assert (
        query_broadening.broadened_query(attempts, current="i j k", goal="") is None
    )


# --------------------------------------------------------------------------
# Case B：连续两次空结果 → 强制加宽，且**不许再加 AND 约束**
# --------------------------------------------------------------------------


def test_two_consecutive_empties_force_a_broadened_query():
    attempts = empty_attempts(*FAILED_QUERIES[:2])
    broadened = query_broadening.broadened_query(
        attempts, current=FAILED_QUERIES[2], goal=FAILED_GOAL
    )
    assert broadened, "连续两次空结果必须给出加宽后的检索词"


def test_broadening_never_adds_constraints_back():
    """钉住这件事：加宽 = **降词数**。

    "A B C D → A B C E → A B D E" 那种换词法是伪降级：结构没变，召回就不会变。
    所以断言的是词数**严格下降**，而不是"换了个词"。
    """
    attempts = empty_attempts(*FAILED_QUERIES[:2])
    current = FAILED_QUERIES[2]
    broadened = query_broadening.broadened_query(attempts, current=current, goal=FAILED_GOAL)

    terms = query_broadening.split_terms(broadened)
    assert len(terms) == 1, f"加宽后的检索词必须只剩一个词，实际 {broadened!r}"
    assert len(terms) < len(query_broadening.split_terms(current))
    assert len(terms) < len(query_broadening.split_terms(FAILED_QUERIES[0]))


def test_term_count_never_increases_for_any_pool():
    """对每一个真机失败查询都成立，而不只是那一个被挑出来的例子。"""
    current = FAILED_QUERIES[3]
    attempts = empty_attempts(*FAILED_QUERIES[:3])
    broadened = query_broadening.broadened_query(attempts, current=current, goal=FAILED_GOAL)

    assert len(query_broadening.split_terms(broadened)) <= len(
        query_broadening.split_terms(current)
    )


def test_a_word_from_the_goal_is_preferred():
    """目标里出现过的词优先——它是**用户自己说的话**，最接近他想看的东西。

    用 ASCII 例子而不是中文词，是为了让这条断言说的是规则本身，
    而不是"应该换成'脱离世俗'"。
    """
    broadened = query_broadening.broadened_query(
        empty_attempts("agents robots 讨论", "机器人 讨论 agents"),
        current="agents 机器人 讨论",
        goal="来一个关于 AI agents 与机器人的话题",
    )
    assert broadened == "agents"


# --------------------------------------------------------------------------
# 不重复：宁可返回 None，也不返回一条注定重复的检索词
# --------------------------------------------------------------------------


def test_an_already_searched_term_is_not_reissued():
    """`脱离世俗` 作为 4 词 AND 的一半没命中，不代表它单独搜也空。

    真机实测它单独搜回来 25 条，所以逐词排除会把最好的候选白白丢掉；
    该排除的是**已经搜过的那条检索词**，不是它切出来的词。
    """
    first = query_broadening.broadened_query(
        empty_attempts(*FAILED_QUERIES[:2]), current=FAILED_QUERIES[2], goal=FAILED_GOAL
    )
    # 假设它单独搜过一次、也空了，于是它进了历史
    second = query_broadening.broadened_query(
        empty_attempts(*FAILED_QUERIES[:2], first),
        current=FAILED_QUERIES[3],
        goal=FAILED_GOAL,
    )
    assert second and second != first


def test_all_candidates_tried_returns_none_instead_of_repeating():
    """每一个候选词都单独搜过且都空了 → 返回 None，不再凑一条重复的。

    这是"多种合理查询都找不到材料"的那一刻：到这儿就该交给上层去收尾，
    而不是继续花预算把同一批词翻来覆去地排列组合。
    """
    tried_alone = [term for query in FAILED_QUERIES for term in query.split()]
    attempts = empty_attempts(*FAILED_QUERIES, *tried_alone)
    assert (
        query_broadening.broadened_query(
            attempts, current=FAILED_QUERIES[3], goal=FAILED_GOAL
        )
        is None
    )


def test_a_broadened_query_is_always_something_x_can_parse():
    """加宽出来的检索词**永远**能原样发给 X。

    这条比"某些词会被丢掉"更要紧：加宽发生在连续两次失败之后，那时再发一次
    `400` 就不是"少取几条材料"，而是整次运行到此为止——恰好是最不该出事的时刻。
    所以这里是**遍历**，而不是挑一个例子。
    """
    nasty = [
        "MCP/Agent 讨论",
        "**重点** A | B",
        "https://t.co/RMhWA6lmX9",
        "A：B",
        "关于 (括号) 的话题",
    ]
    for current in nasty:
        broadened = query_broadening.broadened_query(
            empty_attempts(*nasty[:2]), current=current, goal=FAILED_GOAL
        )
        if broadened is None:
            continue
        assert search_query.usable(broadened), (
            f"{current!r} 加宽成了 X 解析不了的 {broadened!r}"
        )


def test_a_query_that_cannot_be_split_has_nothing_to_broaden():
    attempts = empty_attempts("   ", "  ")
    assert query_broadening.broadened_query(attempts, current="x", goal="") is None


# --------------------------------------------------------------------------
# plan_search：把"选词"和"加宽"接在一起
# --------------------------------------------------------------------------


def test_plan_search_returns_the_chosen_query_untouched_when_not_broadening():
    state = {"search_attempts": []}
    query, note = plan_search(state, [("决策指定的检索词", "AI 智能体 讨论")])
    assert query == "AI 智能体 讨论"
    assert note == "决策指定的检索词"


def test_plan_search_says_in_the_note_that_it_broadened():
    """被换掉的检索词必须能从 note 里看出来。

    与 `search_query.choose()` 的降级同一条理由：summary 里那句"按「X」检索"
    是唯一能说明"我们搜的不是 Planner 说的那个词"的地方。
    """
    state = {"search_attempts": empty_attempts(*FAILED_QUERIES[:2]), "goal": FAILED_GOAL}
    query, note = plan_search(state, [("决策指定的检索词", FAILED_QUERIES[2])])
    assert query != FAILED_QUERIES[2]
    assert "加宽" in note and query in note


# --------------------------------------------------------------------------
# Case E：正常热门题材不受影响
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def run_id(session_factory) -> str:
    async with session_factory() as session:
        run = await create_agent_run(session, None, goal=FAILED_GOAL)
        return str(run.id)


class RecordingXApiClient(FakeXApiClient):
    """前 `empties` 次检索返回空，之后返回真材料。

    真实的失败形态是"连续几次空"，而不是"一直空"——只有前者才需要验证
    "加宽之后流程能接着走"。
    """

    def __init__(self, *, empties: int):
        super().__init__()
        self._empties = empties
        self.queries: list[str] = []

    async def search_recent_tweets(self, query, max_results=10, *, start_time=None, end_time=None):
        self.queries.append(query)
        if len(self.queries) <= self._empties:
            return EMPTY_RESPONSE
        return await super().search_recent_tweets(
            query, max_results, start_time=start_time, end_time=end_time
        )


def discover_tool(client, session_factory, payload=None):
    if payload is None:
        payload = {
            "topics": [
                {
                    "title": "候选选题 1",
                    "description": "一组材料在讲同一件事",
                    "keywords": ["关键词"],
                    "why_now": "刚刚发生",
                    "unknowns": [],
                    # 加宽后那一轮才取回材料，所以 m1/m2 一定来自它。
                    "source_ids": ["m1", "m2"],
                }
            ]
        }
    return make_discover_tool(
        XService(client, clock=lambda: FIXED_NOW),
        ScriptedLLMService(responses={"discover": payload}),
        session_factory,
    )


async def test_a_hot_topic_is_searched_exactly_as_the_planner_asked(session_factory, run_id):
    """第一次搜索就有材料时，检索词**一个字都不改**。

    加宽只在连续空结果时发生——这条钉住"没有把正常流程也改宽"。
    """
    client = RecordingXApiClient(empties=0)
    tool = discover_tool(client, session_factory)
    state = {"run_id": run_id, "goal": FAILED_GOAL}

    result = await tool(state, discover_decision(query="AI 智能体 讨论"))

    assert client.queries == ["AI 智能体 讨论"]
    assert result.delta["search_attempts"] == [
        SearchAttempt(query="AI 智能体 讨论", result_count=2)
    ]


async def test_two_empty_searches_then_the_broadened_one_finds_material(
    session_factory, run_id
):
    """Case C：Q1 → 0，Q2 → 0，Q3 → 材料，流程必须**接着往下走**。

    "加宽之后拿到材料"只是第一步；要验的是它确实回到了原来的流程里
    （discover 出候选选题），而不是提前收尾。
    """
    client = RecordingXApiClient(empties=2)
    tool = discover_tool(client, session_factory)
    state = {"run_id": run_id, "goal": FAILED_GOAL}

    for query in FAILED_QUERIES[:3]:
        result = await tool(state, discover_decision(query=query))
        state.update(result.delta)
        state["candidate_topics"] = result.delta.get(
            "candidate_topics", state.get("candidate_topics")
        )

    assert len(client.queries) == 3
    assert client.queries[:2] == list(FAILED_QUERIES[:2]), "前两次应当原样照搜"
    broadened = client.queries[2]
    assert len(query_broadening.split_terms(broadened)) == 1, (
        f"第三次应当已经被加宽成一个词，实际 {broadened!r}"
    )

    assert [a.result_count for a in state["search_attempts"]] == [0, 0, 2]
    assert state["candidate_topics"], "取回材料之后必须继续产出候选选题"
    assert state["research_item_ids"]


# --------------------------------------------------------------------------
# Case D：全都为空时，安全结束
# --------------------------------------------------------------------------


async def test_all_searches_empty_still_ends_safely(session_factory):
    """Case D：四次全空 → 安全收尾，**不编造材料、不产出伪造正文**。

    这是本轮最重要的一条边界：加宽是为了"多种合理查询都找不到时才结束"，
    而不是"搜不到也必须出稿"。
    """
    client = RecordingXApiClient(empties=99)
    llm = ScriptedLLMService(
        decisions=[decision("discover_topics") for _ in FAILED_QUERIES]
        + [decision("finish")],
        # 模型这一轮**没有**材料可引：给一份空分组，不然它会去引用 m1/m2，
        # 而那些编号在"一条材料都没取回"的池子里根本不存在。
        responses={"discover": {"topics": []}},
    )
    x_service = XService(client, clock=lambda: FIXED_NOW)

    async with runtime_with_checkpointer(
        session_factory, x_service=x_service, llm_service=llm
    ) as runtime:
        async with session_factory() as session:
            run = await create_agent_run(session, None, goal=FAILED_GOAL)
        run_id = str(run.id)
        config = {"configurable": {"thread_id": run_id}}

        with pytest.raises(AgentIncompleteError) as excinfo:
            await runtime.graph.ainvoke({"run_id": run_id, "goal": FAILED_GOAL}, config=config)

        assert "未产出任何候选正文" in str(excinfo.value)
        assert "材料 0 条" in str(excinfo.value)
        snapshot = await runtime.graph.aget_state(config)

    # 四次检索都发生了，而且**后两次已经加宽**（用光预算，不是重复同一组词）
    assert len(client.queries) == 4
    assert len(query_broadening.split_terms(client.queries[0])) > 1
    assert all(
        len(query_broadening.split_terms(q)) == 1 for q in client.queries[2:]
    ), f"连续两次空结果之后必须加宽，实际 {client.queries!r}"
    # 没有材料、没有候选、没有正文：空就是空，不补一个假的
    assert snapshot.values.get("research_item_ids") in (None, [])
    assert not snapshot.values.get("candidates")
    assert not snapshot.values.get("draft_content")
    assert snapshot.values["search_attempts"], "每一次检索都要留下记录"
