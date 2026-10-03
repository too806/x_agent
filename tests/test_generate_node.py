import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.agent.nodes.generate import build_prompt, make_generate_node
from app.agent.persona import Persona
from app.models import Draft
from app.repositories import create_agent_run
from app.schemas import ResearchResult
from app.services.llm import LLMError
from fakes import FakeLLMService, draft_json

PERSONA = Persona(
    name="default",
    description="简洁专业",
    tone="conversational",
    style=["简洁", "准确"],
    max_length=280,
)


async def test_generate_node_validates_persists_and_injects_context(session_factory):
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)

    results = [
        ResearchResult(
            platform_post_id="1001",
            content="research material here",
            author_username="ada_dev",
        )
    ]
    llm = FakeLLMService(text=draft_json(content="A concise post."))
    node = make_generate_node(llm, PERSONA, session_factory)

    out = await node(
        {"run_id": run_id, "topic": "AI coding agent", "research_results": results}
    )

    assert out["draft_content"] == "A concise post."
    assert out["draft_id"]

    # 结构化输出：LLM 被要求 JSON 模式，且 prompt 注入了 persona 与研究材料
    call = llm.calls[0]
    assert call["json_mode"] is True
    assert "conversational" in call["prompt"]
    assert "research material here" in call["prompt"]

    # draft 已持久化（version=1，含 model_name / prompt_version）
    async with session_factory() as session:
        drafts = (await session.execute(select(Draft))).scalars().all()
        assert len(drafts) == 1
        d = drafts[0]
        assert d.content == "A concise post."
        assert d.version == 1
        assert d.prompt_version == "2"
        assert d.model_name


async def test_generate_node_wraps_invalid_json_as_llm_error(session_factory):
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)

    node = make_generate_node(FakeLLMService(text="not-json"), PERSONA, session_factory)
    with pytest.raises(LLMError) as excinfo:
        await node({"run_id": run_id, "topic": "AI coding agent", "research_results": []})

    # 原始 ValidationError 作为 cause 保留，便于诊断
    assert isinstance(excinfo.value.__cause__, ValidationError)


async def test_generate_node_wraps_schema_mismatch_as_llm_error(session_factory):
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)

    # 合法 JSON，但缺少必填字段 content/hook_type/topic
    node = make_generate_node(FakeLLMService(text='{"foo": "bar"}'), PERSONA, session_factory)
    with pytest.raises(LLMError) as excinfo:
        await node({"run_id": run_id, "topic": "AI coding agent", "research_results": []})

    assert isinstance(excinfo.value.__cause__, ValidationError)
    assert "content" in str(excinfo.value)  # 诊断信息保留

    async with session_factory() as session:
        assert (await session.execute(select(Draft))).scalars().all() == []


def test_build_prompt_replaces_tokens():
    persona = PERSONA
    results = [ResearchResult(platform_post_id="1", content="fact one", author_username="u")]
    prompt = build_prompt("topicX", results, persona)
    assert "topicX" in prompt
    assert "fact one" in prompt
    assert "conversational" in prompt
    assert "280" in prompt
    assert "{{" not in prompt  # 所有占位符已替换
