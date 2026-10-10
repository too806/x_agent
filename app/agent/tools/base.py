"""Tool 的统一接口与共用的执行外壳。

工作单第六节：Tool **不是**独立服务，而是通过统一接口供 Planner 调用的可调用对象。
这里只有一个约定：

    tool(state, decision) -> ToolResult(delta, summary)

- `delta` 是要并回 Graph state 的字段；
- `summary` 是一句话说明这一步实际产出了什么，写进 decision trace。

把两者分开是刻意的：state 是给**下一次决策**用的（结构化的），
summary 是给**人看的**（一句话）。混在一起会让 prompt 里塞满调试信息。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from pydantic import BaseModel, ValidationError

from app.agent import query_broadening, search_query
from app.agent.schemas import AgentDecision, SearchAttempt
from app.services.llm import LLMError, LLMService

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    delta: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


Tool = Callable[[Mapping[str, Any], AgentDecision], Awaitable[ToolResult]]


#: 出错时回显多少原始输出。pydantic 只把非法输入截成 `<前 10 字>...<后 10 字>`，
#: 而真正坏掉的地方通常在那中间——回显一段原文，才可能判断是模型输出了半截 JSON、
#: 还是某个字符串里有没转义的引号。200 字够看出结构，又不至于把整份输出灌进日志。
ERROR_SNIPPET_CHARS = 200

#: 结构化输出的尝试次数上限（含第一次）。**固定一次重试，不是可配置的重试策略**：
#: 真机 run `9876ea27` 暴露的故障是"`reasoning` 里嵌了一对没转义的 ASCII 引号"，
#: 属于概率性的手滑，让模型完整重写一遍通常就好；而真正坏掉的输出，第三次也不会变好，
#: 只会把一次 run 的成本乘上去。所以是 `for` 而不是 `while`，没有第三次。
MAX_STRUCTURED_OUTPUT_ATTEMPTS = 2

#: 校验失败的两种类别，取自 pydantic 自己的 `type`。分开是为了**重试时说对话**，
#: 也是为了让最终错误可分类：`json_invalid` 是 JSON 语法坏（真机实证的报错是
#: `expected ',' or '}'`），`schema_invalid` 是 JSON 合法但字段或类型不对。
#: 前者的提示要讲引号转义，后者的要讲字段与类型，两句话互不适用。
JSON_INVALID = "json_invalid"
SCHEMA_INVALID = "schema_invalid"

#: 重试时追加的提示。**刻意只加这几句，绝不回灌上一次的原始输出**：
#: 回灌既要把一段已知会诱发同样错误的样本再喂一遍，也得多花一次输入 token，
#: 而模型需要知道的只是"哪里不合规"，不是"你上次写成了什么样"。
_JSON_RETRY_HINT = (
    "\n\n上一次输出不是合法 JSON。请重新输出完整 JSON。字符串值内部不要使用"
    "裸 ASCII 双引号；需要引用短语时使用中文引号「」或“”，或直接不加引号。"
    "只输出 JSON。"
)
_SCHEMA_RETRY_HINT = (
    "\n\n上一次输出不符合要求的 JSON 结构。请重新输出完整 JSON，"
    "严格遵守字段和类型要求。只输出 JSON。"
)


def _validation_kind(exc: ValidationError) -> str:
    """把一次 pydantic 校验失败归到 `json_invalid` 或 `schema_invalid`。

    只读 pydantic 自己给的 `type`，不重写异常体系：JSON 语法错时它就报
    `json_invalid`（见 `app/services/llm.py` 里记的那次真机现象）。
    """
    for error in exc.errors():
        if error.get("type") == JSON_INVALID:
            return JSON_INVALID
    return SCHEMA_INVALID


async def complete_json(
    llm_service: LLMService, prompt: str, model: type[BaseModel]
) -> BaseModel:
    """调 LLM 并把结果校验成给定模型；结构化输出失败时**重试一次**。

    非法 JSON 或不符合 schema 都属于**上游输出故障**，与 LLM 调用失败同类，
    统一包装成 LLMError（保留原始诊断信息，且不含任何凭据）。

    重试只包住 `model_validate_json` 这一层。**`llm_service.complete` 自身的异常
    （超时 / 网络 / 限流 / 被输出上限截断 / 空内容）一律原样穿出，绝不重试**：
    重试同一份 prompt 必然撞上同一个上限，那只是把一次故障变成两次调用，还会与
    更外层已有的重试叠加成风暴。截断有它自己的信号和消息
    （见 `OpenAILLMService.complete`），不需要在这里再判一次。

    重试**不做任何 JSON 修复**——不补括号、不补引号、不 strip 代码块、不截取坏输出的
    前半段。修出来的 JSON 未必是模型想说的意思，而且会把"模型有多常在写坏 JSON"
    这个真实故障率藏起来。坏了就让它完整重写。

    错误信息里带上模型原始输出的一段（见 `ERROR_SNIPPET_CHARS`）：真机 smoke 里
    出现过一次 planner 返回非法 JSON，而 pydantic 的报错只显示了首尾各十个字符，
    中间坏在哪完全看不出来——**下一次再遇到，至少要先看得见**。
    """
    last: LLMError | None = None
    for attempt in range(1, MAX_STRUCTURED_OUTPUT_ATTEMPTS + 1):
        raw = await llm_service.complete(prompt, json_mode=True)
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            kind = _validation_kind(exc)
            snippet = " ".join(raw.split())[:ERROR_SNIPPET_CHARS]
            last = LLMError(
                f"LLM returned invalid {model.__name__} "
                f"({kind}, attempt {attempt}/{MAX_STRUCTURED_OUTPUT_ATTEMPTS}): "
                f"{exc}\nraw: {snippet}"
            )
            if attempt == MAX_STRUCTURED_OUTPUT_ATTEMPTS:
                raise last from exc
            logger.warning(
                "structured output invalid, retrying once (model=%s, kind=%s): %s",
                model.__name__,
                kind,
                snippet,
            )
            prompt = prompt + (
                _JSON_RETRY_HINT if kind == JSON_INVALID else _SCHEMA_RETRY_HINT
            )
    assert last is not None  # 只有校验失败才会走到循环之外
    raise last


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


def plan_search(state: Mapping[str, Any], options: Sequence[tuple[str, str]]) -> tuple[str, str]:
    """这一轮**实际**要交给 X 的检索词，以及它是怎么来的。

    两步，管的是两件不同的事：

    1. `search_query.choose()`——**字符合法性**。有些词会让 X 直接 400
       （URL 里的 `/`、标题里的 `|`），而那是整次运行到此为止，不是少取几条。
    2. `query_broadening.broadened_query()`——**召回**。连续两次一条材料都没
       取回时，问题就不在"词选得准不准"而在"约束太多"（X 把空格当 AND），
       此时必须把词数降下来，而不是换一组同样窄的词。

    第二步只在**连续空结果**时发生，所以正常跑通的运行完全不受影响：
    第一次搜索就取回材料的话，这里原样返回 Planner 给的词。
    """
    query, note = search_query.choose(options)
    attempts = state.get("search_attempts") or []
    broadened = query_broadening.broadened_query(
        attempts,
        current=query,
        goal=str(state.get("goal") or state.get("topic") or ""),
    )
    if broadened is None:
        return query, note

    empties = query_broadening.consecutive_empty(attempts)
    # 与 `search_query.choose()` 的降级一样必须**留痕**：检索词被换掉之后，
    # summary 里那句"按「X」检索"是唯一能说明"我们搜的不是 Planner 说的那个词"
    # 的地方，而"连续空结果 → 加宽"正是事后要复盘的那条路径。
    logger.warning(
        "broadening search query after consecutive empty results "
        "(empties=%d, was=%r, now=%r)",
        empties,
        query,
        broadened,
    )
    return broadened, f"{note}（连续 {empties} 次空结果，已加宽为「{broadened}」）"


def append_attempts(state: Mapping[str, Any], query: str, result_count: int) -> dict[str, Any]:
    """把这一次检索（检索词 + 取回条数）追加到 state。

    `search_attempts` 只由本函数改动——与 `append_research` 对 `research_results`
    的约定同一条理由：不变量只在一个地方维护，才不会有人漏记一次。

    "取回 0 条"单独记一条日志：它是 `plan_search` 下一步会不会加宽的**唯一输入**，
    而"这次怎么没取到材料"与"最近确实没什么可讨论的"在结果上长得一模一样，
    事后复盘只能靠这行日志把两者分开。
    """
    if result_count == 0:
        logger.info("search query returned 0 results (query=%r)", query)
    return {
        "search_attempts": list(state.get("search_attempts") or [])
        + [SearchAttempt(query=query, result_count=result_count)]
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
