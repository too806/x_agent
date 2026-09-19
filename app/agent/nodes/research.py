import logging

from app.agent.state import RunState
from app.repositories import create_research_items
from app.schemas import ResearchResult
from app.services.x_service import XService

logger = logging.getLogger(__name__)


def make_research_node(x_service: XService, session_factory):
    async def research_node(state: RunState) -> dict:
        run_id = state["run_id"]
        topic = state["topic"]
        logger.info("research started (run_id=%s)", run_id)
        results: list[ResearchResult] = await x_service.research(topic)
        async with session_factory() as session:
            item_ids = await create_research_items(session, run_id, results)
        logger.info("research completed (run_id=%s, items=%d)", run_id, len(results))
        return {"research_results": results, "research_item_ids": item_ids}

    return research_node
