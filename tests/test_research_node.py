from sqlalchemy import select

from app.agent.nodes.research import make_research_node
from app.models import ResearchItem
from app.repositories import create_agent_run
from app.services.x_service import XService
from fakes import FakeXApiClient


async def test_research_node_persists_and_returns(session_factory):
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)

    node = make_research_node(XService(FakeXApiClient()), session_factory)
    out = await node({"run_id": run_id, "topic": "AI coding agent"})

    assert len(out["research_results"]) == 2
    assert len(out["research_item_ids"]) == 2

    async with session_factory() as session:
        rows = (await session.execute(select(ResearchItem))).scalars().all()
        assert len(rows) == 2
        assert {str(r.agent_run_id) for r in rows} == {run_id}
