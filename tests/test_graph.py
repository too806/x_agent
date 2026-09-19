from sqlalchemy import select

from app.agent.graph import GraphDeps, build_graph
from app.agent.persona import Persona
from app.models import Draft, ResearchItem
from app.repositories import create_agent_run
from app.services.x_service import XService
from fakes import FakeLLMService, FakeXApiClient, draft_json

PERSONA = Persona(
    name="default",
    description="简洁专业",
    tone="professional",
    style=["简洁"],
    avoid=["夸大"],
    max_length=280,
)


async def test_graph_full_flow(session_factory):
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)

    deps = GraphDeps(
        x_service=XService(FakeXApiClient()),
        llm_service=FakeLLMService(text=draft_json()),
        persona=PERSONA,
        session_factory=session_factory,
    )
    graph = build_graph(deps)
    final = await graph.ainvoke({"run_id": run_id, "topic": "AI coding agent"})

    assert final["topic"] == "AI coding agent"
    assert len(final["research_results"]) == 2
    assert len(final["research_item_ids"]) == 2
    assert final["draft_content"]
    assert final["draft_id"]

    async with session_factory() as session:
        items = (await session.execute(select(ResearchItem))).scalars().all()
        drafts = (await session.execute(select(Draft))).scalars().all()
        assert len(items) == 2
        assert len(drafts) == 1
