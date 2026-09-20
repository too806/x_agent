from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.nodes.generate import make_generate_node
from app.agent.nodes.guardrail import make_guardrail_node
from app.agent.nodes.human_review import make_human_review_node
from app.agent.nodes.research import make_research_node
from app.agent.persona import Persona
from app.agent.state import RunState
from app.services.llm import LLMService
from app.services.x_service import XService


@dataclass
class GraphDeps:
    x_service: XService
    llm_service: LLMService
    persona: Persona
    session_factory: async_sessionmaker[AsyncSession]


def build_graph(deps: GraphDeps, checkpointer):
    """编译 Graph：START → research → generate → guardrail → human_review → END。

    checkpointer 是必须的：human_review 用 interrupt() 暂停，没有 checkpointer 时
    LangGraph 无法保存/恢复状态。
    """
    research_node = make_research_node(deps.x_service, deps.session_factory)
    generate_node = make_generate_node(deps.llm_service, deps.persona, deps.session_factory)
    guardrail_node = make_guardrail_node(deps.persona, deps.session_factory)
    human_review_node = make_human_review_node(deps.session_factory)

    graph = StateGraph(RunState)
    graph.add_node("research", research_node)
    graph.add_node("generate", generate_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("human_review", human_review_node)
    graph.add_edge(START, "research")
    graph.add_edge("research", "generate")
    graph.add_edge("generate", "guardrail")
    graph.add_edge("guardrail", "human_review")
    graph.add_edge("human_review", END)
    return graph.compile(checkpointer=checkpointer)
