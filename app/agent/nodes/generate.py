import logging
from pathlib import Path

from pydantic import ValidationError

from app.agent.nodes.finalize import AgentOverlongError
from app.agent.persona import Persona
from app.agent.post_structure import length_detail, over_hard_limit
from app.agent.prompts import format_persona
from app.agent.state import RunState
from app.config import settings
from app.repositories import create_draft
from app.schemas import GeneratedDraft, ResearchResult
from app.services.llm import LLMError, LLMService

logger = logging.getLogger(__name__)

#: V1（goal 为空）的 prompt 版本。与 Agent 链路的 "5b" 区分开。
#: 2：补上阅读契约（首句钩子 / 短段落 / 不说编号 / 不许编造）。
PROMPT_VERSION = "2"


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
        template.replace("{{PERSONA}}", format_persona(persona))
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

        # 交付硬上限：与 Agent 链路（finalize）、发布预检读的是**同一个尺子**
        # （`app/agent/post_structure.over_hard_limit`，`len()` 口径），上限取
        # 运行时真正生效的那一份（`persona.max_length`，生产来自
        # `config/persona.yaml` 的 140）——这里不写第二个数字，也不自己数字数。
        #
        # 为什么拦在**建 draft 之前**：draft 是下游（人工审核队列、发布预检、
        # 控制台）判断"这次 Run 有没有东西可以审"的唯一信号。先写一条超长 draft
        # 再拒绝，等于让审核队列里出现一条谁都没打算发布的稿子；而审核者要么
        # 替它删字（那不是审核，是替它写稿），要么没看见就发出去（更糟）。
        # 所以"没有任何可交付的正文"是这件事的**真实状态**，要如实说出来。
        #
        # 为什么不截断：砍掉末尾凑合规会造成句子残缺、观点失真、事实关系破坏，
        # 那是把不合格伪装成合格的假象，比超长更糟。
        #
        # 为什么是失败而不是自动改写：V1 没有候选池，也没有改写回路。补齐这条
        # 约束不该顺手给 V1 加一次 LLM 调用或一个新的 Agent 节点——那是扩大
        # V1 的智能能力，不是统一交付约束。要加自动改写是另一件事。
        #
        # 复用 `AgentOverlongError`（它继承 `AgentIncompleteError`）：对下游而言
        # 这两条链路是同一件事——"这次 Run 没有可交付的正文"。因此 Run 落
        # `failed`、错误信息带确切字数、控制台读到的东西都与 Agent 链路一致，
        # `auto_review` / 自动发布那边也已经认这个类型，不必再加一条分支。
        if over_hard_limit(draft.content, persona.max_length):
            raise AgentOverlongError(
                "V1 生成的正文超过交付硬上限："
                f"{length_detail(draft.content, persona.max_length)}。"
                "V1 链路没有自动改写，这条 Run 以失败收场：没有写入 draft，"
                "也没有进入人工审核。"
            )

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
