from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.nodes.generate import make_generate_node
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


def build_graph(deps: GraphDeps):
    research_node = make_research_node(deps.x_service, deps.session_factory)
    generate_node = make_generate_node(deps.llm_service, deps.persona, deps.session_factory)

    graph = StateGraph(RunState)
    graph.add_node("research", research_node)
    graph.add_node("generate", generate_node)
    graph.add_edge(START, "research")
    graph.add_edge("research", "generate")
    graph.add_edge("generate", END)
    return graph.compile()
