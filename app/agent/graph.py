"""LangGraph 拓扑：V1 的固定链路与 Phase 5A 的 Agentic 循环共用一张图。

## 为什么是一张图而不是两张

两套拓扑的下游完全一样：guardrail → human_review → END，而 `PublishService`
在核对 checkpoint 时用的是**运行这条 Run 的那张图**。分成两张图意味着每个持有
graph 的地方（Runtime、PublishService、测试的 runtime 构造）都要多问一句
"这条 Run 是哪张图的"，而这个判断一旦有一处不一致，发布时的交叉核对就会
拿另一张图的状态去比——那是最不该出错的地方。

所以：**在 START 处分岔**，用 `goal` 是否为 None 判别（判别式的唯一真相来源，
理由见 app/agent/state.py）。

    START ─┬─ goal is None ─→ research → generate ─┐
           └─ goal 非空 ───→ planner ⇄ tools       ├→ guardrail → human_review → END
                              └─ finish → finalize ─┘

## Phase 5A 的那一半

`planner` 按 `AgentDecision.action` 分岔到对应的执行节点，每个执行节点回到
`planner`——这是一个**循环**，不是链。终止条件由预算保证（app/agent/budget.py）：
`finish` 是唯一的出口，而它在 `FALLBACK_ORDER` 末尾，所以任何一条路径都会在
有限步内到达 finalize。

刻意**不给** generate_candidates → criticize 设固定边：工作单第四节把 `criticize`
列为 Planner 的动作之一，固定边会让这个动作永远轮不到 Planner 选，schema 就成了
摆设。链路仍然强制——"候选还没被评价过时不许 finish"由
`budget.ready_to_finish` 保证，那是确定性的，不依赖模型是否听话。
"""

from dataclasses import dataclass
from typing import Mapping

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.budget import AgentBudget, load_budget
from app.agent.nodes.execute import make_tool_node
from app.agent.nodes.finalize import make_finalize_node
from app.agent.nodes.generate import make_generate_node
from app.agent.nodes.guardrail import make_guardrail_node
from app.agent.nodes.human_review import make_human_review_node
from app.agent.nodes.planner import make_planner_node
from app.agent.nodes.research import make_research_node
from app.agent.persona import Persona
from app.agent.schemas import AgentAction
from app.agent.state import RunState
from app.agent.tools import build_tools
from app.services.llm import LLMService
from app.services.x_service import XService


@dataclass
class GraphDeps:
    x_service: XService
    llm_service: LLMService
    persona: Persona
    session_factory: async_sessionmaker[AsyncSession]


# 由工具执行的动作（`finish` 不是工具，它走 finalize）。
# 名字顺序即 Graph 里注册的节点名顺序，节点名 = 动作值，于是 checkpoint 里
# 每一步的节点名就是那个动作名，人读起来不用再对照一张表。
TOOL_ACTIONS: tuple[AgentAction, ...] = (
    AgentAction.DISCOVER_TOPICS,
    AgentAction.SELECT_TOPIC,
    AgentAction.RESEARCH_TOPIC,
    AgentAction.DEEPEN_RESEARCH,
    AgentAction.EXTRACT_EVIDENCE,
    AgentAction.GENERATE_ANGLES,
    AgentAction.SELECT_ANGLE,
    AgentAction.GENERATE_CANDIDATES,
    AgentAction.CRITICIZE,
    AgentAction.REVISE,
)

FINALIZE = "finalize"


def _entry_route(state: Mapping) -> str:
    """START 分岔：有 goal 走 Agent 循环，否则走原来的固定链路。"""
    return "planner" if state.get("goal") else "research"


def _make_action_route():
    """按 Planner 的决策分岔。

    决策本身必然存在——`planner` 节点是它唯一的上游，而那个节点总会写
    `decision`。缺少决策时说明图被以非法方式进入了，此时报错比默认走某一条
    分支更安全：默认分支会让一次错误的调用看起来像一次正常的运行。
    """

    def route(state: Mapping) -> str:
        decision = state.get("decision")
        if decision is None:  # pragma: no cover - 结构上不可达
            raise RuntimeError("planner 节点没有产出 decision，无法决定下一步")
        if decision.action is AgentAction.FINISH:
            return FINALIZE
        return decision.action.value

    return route


def build_graph(deps: GraphDeps, checkpointer, budget: AgentBudget | None = None):
    """编译 Graph。

    checkpointer 是必须的：human_review 用 interrupt() 暂停，没有 checkpointer 时
    LangGraph 无法保存/恢复状态。

    budget 可注入是为了测试：预算参数来自 config/agent.yaml，而测试需要把它压到
    很小（例如 max_steps=3）来验证"预算用尽会停下来"——不注入的话，
    验证终止性就得真的跑 12 步。
    """
    budget = budget or load_budget()
    tools = build_tools(deps, budget)
    planner_node = make_planner_node(deps.llm_service, deps.session_factory, budget)

    graph = StateGraph(RunState)

    # --- V1 链路（Phase 1–4，语义未改）---
    graph.add_node("research", make_research_node(deps.x_service, deps.session_factory))
    graph.add_node("generate", make_generate_node(deps.llm_service, deps.persona, deps.session_factory))

    # --- Phase 5A 循环 ---
    graph.add_node("planner", planner_node)
    for action in TOOL_ACTIONS:
        graph.add_node(action.value, make_tool_node(action, tools, deps.session_factory))
        graph.add_edge(action.value, "planner")  # 执行完回到 Planner：这是循环
    graph.add_node(FINALIZE, make_finalize_node(deps.session_factory, deps.persona))
    graph.add_edge(FINALIZE, "guardrail")

    # --- 两条链路的汇合点 ---
    guardrail_node = make_guardrail_node(deps.persona, deps.session_factory)
    human_review_node = make_human_review_node(deps.session_factory)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("human_review", human_review_node)
    graph.add_edge("guardrail", "human_review")
    graph.add_edge("human_review", END)

    graph.add_conditional_edges(
        START, _entry_route, {"planner": "planner", "research": "research"}
    )
    graph.add_edge("research", "generate")
    graph.add_edge("generate", "guardrail")
    graph.add_conditional_edges(
        "planner",
        _make_action_route(),
        {**{action.value: action.value for action in TOOL_ACTIONS}, FINALIZE: FINALIZE},
    )

    return graph.compile(checkpointer=checkpointer)
