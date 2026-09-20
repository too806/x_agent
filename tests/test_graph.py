from langgraph.types import Command
from sqlalchemy import select

from app.models import Draft, ResearchItem, Review
from app.repositories import create_agent_run
from fakes import FakeLLMService, draft_json
from support import runtime_with_checkpointer


async def _start(session_factory) -> str:
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        return str(run.id)


async def test_graph_stops_at_human_review(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        run_id = await _start(session_factory)
        config = {"configurable": {"thread_id": run_id}}

        out = await runtime.graph.ainvoke(
            {"run_id": run_id, "topic": "AI coding agent"}, config=config
        )

        assert "__interrupt__" in out
        payload = out["__interrupt__"][0].value
        assert payload["type"] == "content_review"
        assert payload["run_id"] == run_id
        assert payload["draft_id"] == out["draft_id"]
        assert payload["content"] == out["draft_content"]
        assert payload["guardrail_passed"] is True
        assert payload["guardrail_reasons"] == []

        snapshot = await runtime.graph.aget_state(config)
        assert snapshot.next == ("human_review",)

        # Research / Generate 的产物已落库，Review 尚未产生
        async with session_factory() as session:
            assert len((await session.execute(select(ResearchItem))).scalars().all()) == 2
            assert len((await session.execute(select(Draft))).scalars().all()) == 1
            assert (await session.execute(select(Review))).scalars().all() == []


async def test_graph_reaches_human_review_even_when_guardrail_fails(session_factory):
    """Guardrail 是提示性的：判定不通过也照样交给人来定夺。"""
    llm = FakeLLMService(text=draft_json(content=""))
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id = await _start(session_factory)
        config = {"configurable": {"thread_id": run_id}}

        out = await runtime.graph.ainvoke(
            {"run_id": run_id, "topic": "AI coding agent"}, config=config
        )

        assert out["guardrail_passed"] is False
        assert out["guardrail_reasons"] == ["empty_content"]
        assert "__interrupt__" in out
        assert out["__interrupt__"][0].value["guardrail_passed"] is False
        assert out["__interrupt__"][0].value["guardrail_reasons"] == ["empty_content"]


async def test_graph_resumes_and_applies_decision(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        run_id = await _start(session_factory)
        config = {"configurable": {"thread_id": run_id}}

        interrupted = await runtime.graph.ainvoke(
            {"run_id": run_id, "topic": "AI coding agent"}, config=config
        )
        resumed = await runtime.graph.ainvoke(
            Command(resume={"action": "edit", "content": "人工改写后的正文"}),
            config=config,
        )

        assert resumed["human_action"] == "edit"
        assert resumed["final_content"] == "人工改写后的正文"
        # 原草稿未被覆盖
        assert resumed["draft_content"] == interrupted["draft_content"]
        assert resumed["draft_content"] != resumed["final_content"]

        snapshot = await runtime.graph.aget_state(config)
        assert snapshot.next == ()

        async with session_factory() as session:
            review = (await session.execute(select(Review))).scalars().one()
            assert review.status == "edit"
            assert review.original_content == interrupted["draft_content"]
            assert review.edited_content == "人工改写后的正文"
