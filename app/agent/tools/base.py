"""Tool 的统一接口与共用的执行外壳。

工作单第六节：Tool **不是**独立服务，而是通过统一接口供 Planner 调用的可调用对象。
这里只有一个约定：

    tool(state, decision) -> ToolResult(delta, summary)

- `delta` 是要并回 Graph state 的字段；
- `summary` 是一句话说明这一步实际产出了什么，写进 decision trace。

把两者分开是刻意的：state 是给**下一次决策**用的（结构化的），
summary 是给**人看的**（一句话）。混在一起会让 prompt 里塞满调试信息。
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from pydantic import BaseModel, ValidationError

from app.agent.schemas import AgentDecision
from app.services.llm import LLMError, LLMService


@dataclass
class ToolResult:
    delta: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


Tool = Callable[[Mapping[str, Any], AgentDecision], Awaitable[ToolResult]]


async def complete_json(
    llm_service: LLMService, prompt: str, model: type[BaseModel]
) -> BaseModel:
    """调一次 LLM 并把结果校验成给定模型。

    与 Phase 2 的 generate 节点同一套处理：非法 JSON 或不符合 schema 都属于
    **上游输出故障**，与 LLM 调用失败同类，统一包装成 LLMError
    （保留原始诊断信息，且不含任何凭据）。
    """
    raw = await llm_service.complete(prompt, json_mode=True)
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise LLMError(f"LLM returned invalid {model.__name__}: {exc}") from exc


def append_research(
    state: Mapping[str, Any], results: list, item_ids: list[str]
) -> dict[str, Any]:
    """把新取回的研究材料**追加**到 state。

    `research_results` 与 `research_item_ids` 必须长度一致、下标一一对应——
    `sources()` 依赖这个不变量来还原"每条材料在数据库里的 id"。
    因此这两个列表只由本函数改动，别处一律不许直接写 state 的这两个键。

    追加而不是覆盖：discovery 阶段取回的材料与后续 topics 研究取回的材料
    都是这次运行的知识来源，覆盖会让前面那批虽然还在库里、却不再出现在任何
    prompt 里——而 Evidence 引用的 source_id 恰恰可能来自它们。
    """
    return {
        "research_results": list(state.get("research_results") or []) + list(results),
        "research_item_ids": list(state.get("research_item_ids") or []) + list(item_ids),
    }


def pairs(item_ids: list[str], results: list) -> list[tuple[str, str]]:
    """(research_item id, 正文) 对。"""
    return [(item_id, r.content) for item_id, r in zip(item_ids, results)]


def sources(state: Mapping[str, Any]) -> list[tuple[str, str]]:
    """state 里全部 (id, 正文) 对，供 prompt 渲染与证据追溯性校验使用。

    `zip` 会在长度不一致时静默截断。这里**刻意保留**这个行为：万一不变量被破坏
    （外部写坏了 state），宁可少给几行材料，也不能让 zip 与另一个列表错位配对——
    那会让证据指向**另一条**帖子，而且事后核对时无法发现。
    """
    return pairs(state.get("research_item_ids") or [], state.get("research_results") or [])


def known_source_ids(state: Mapping[str, Any]) -> set[str]:
    return {item_id for item_id, _ in sources(state)}
