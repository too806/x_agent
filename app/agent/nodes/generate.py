import logging
from pathlib import Path

from pydantic import ValidationError

from app.agent.persona import Persona
from app.agent.state import RunState
from app.config import settings
from app.repositories import create_draft
from app.schemas import GeneratedDraft, ResearchResult
from app.services.llm import LLMError, LLMService

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1"


def _format_persona(persona: Persona) -> str:
    return (
        f"- 名称：{persona.name}\n"
        f"- 描述：{persona.description}\n"
        f"- 语气：{persona.tone}\n"
        f"- 风格：{'；'.join(persona.style)}\n"
        f"- 避免：{'；'.join(persona.avoid)}"
    )


def _format_research(results: list[ResearchResult]) -> str:
    if not results:
        return "（无研究材料）"
    lines = []
    for i, r in enumerate(results, 1):
        meta = f"likes={r.likes}, reposts={r.reposts}, replies={r.replies}"
        if r.author_username:
            meta = f"@{r.author_username} {meta}"
        url = f" 来源：{r.url}" if r.url else ""
        lines.append(f"{i}. {r.content}{url}（{meta}）")
    return "\n".join(lines)


def build_prompt(topic: str, results: list[ResearchResult], persona: Persona) -> str:
    template = Path(settings.prompt_path).read_text(encoding="utf-8")
    return (
        template.replace("{{PERSONA}}", _format_persona(persona))
        .replace("{{TOPIC}}", topic)
        .replace("{{RESEARCH}}", _format_research(results))
        .replace("{{MAX_LENGTH}}", str(persona.max_length))
    )


def make_generate_node(llm_service: LLMService, persona: Persona, session_factory):
    async def generate_node(state: RunState) -> dict:
        run_id = state["run_id"]
        topic = state["topic"]
        results = state.get("research_results", [])
        logger.info("generation started (run_id=%s)", run_id)
        prompt = build_prompt(topic, results, persona)
        raw = await llm_service.complete(prompt, json_mode=True)
        try:
            draft = GeneratedDraft.model_validate_json(raw)
        except ValidationError as exc:
            # LLM 返回非法 JSON 或不符合 schema：属于上游输出故障，与 LLM 调用失败同类，
            # 统一包装为 LLMError（保留原始诊断信息，且不含任何凭据）。
            raise LLMError(f"LLM returned invalid draft structure: {exc}") from exc
        async with session_factory() as session:
            draft_id = await create_draft(
                session,
                run_id=run_id,
                content=draft.content,
                version=1,
                prompt_version=PROMPT_VERSION,
                model_name=settings.llm_model,
            )
        logger.info("generation completed (run_id=%s, draft_id=%s)", run_id, draft_id)
        return {"draft_content": draft.content, "draft_id": draft_id}

    return generate_node
