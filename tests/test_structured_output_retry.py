"""`complete_json` 的结构化输出重试：一次，且只包校验那一层。

背景是真机 run `9876ea27`：`AngleDraftList` 的 `reasoning` 里嵌了一对没转义的
ASCII 双引号，JSON 在列 204 处被提前闭合（`json_invalid`），一次坏输出就把整个
run 作废了。修法是在 `complete_json` 里固定重试一次——**不是可配置的重试策略**。

这个文件钉住四件不能退化的事：

1. 语法坏与 schema 坏各重试一次，且重试时说对话（引号转义 vs 字段类型）；
2. 基础设施故障（截断 / 网络 / 限流）**一次都不重试**——重试同一份 prompt
   只会撞上同一个上限；
3. 重试的 prompt 里**不回灌**上一次的原始输出；
4. 不改坏输出，也不从坏输出里抠字段。
"""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.agent.schemas import AngleDraftList
from app.agent.tools.base import (
    MAX_STRUCTURED_OUTPUT_ATTEMPTS,
    complete_json,
)
from app.services.llm import LLMError, OpenAILLMService

# ---------------------------------------------------------------------------
# 真机样本：run 9876ea27 实际返回的那段非法 JSON（取自 agent_run.error 的 raw 回显）
#
# `reasoning` 的值里嵌着一对裸 ASCII 双引号（`是"可写代码、修 bug 的常驻智能体"`），
# 于是字符串在列 204 处被提前闭合，解析器紧接着遇到「可」而不是 `,` 或 `}`。
# 本地探针复现过：这段文本给出的报错就是 `expected ',' or '}' at line 1 column 204`。
# **它不是截断**——同样的文本裁断在同一个位置，报的是 `EOF while parsing a string`。
# ---------------------------------------------------------------------------
REAL_MALFORMED_ANGLES = (
    '{"angles": [{"claim": "常驻编码智能体一旦真的常驻，卡住它的不会是代码写得好不好，'
    '而是人能不能在事后复核它到底改了什么。", "reasoning": "f1 只说 dots 是'
    '"可写代码、修 bug 的常驻智能体"，f2 显示"}]}'
)

#: 同一个片段里最独特的一句，用来断言"重试时没有把它回灌给模型"。
RAW_ONLY_FRAGMENT = "可写代码、修 bug 的常驻智能体"

VALID_ANGLES = json.dumps(
    {"angles": [{"claim": "常驻智能体会产生没人复核的改动。", "evidence_ids": ["f1"]}]},
    ensure_ascii=False,
)

#: JSON 合法、但类型不对（`angles` 应是数组）。归到 schema_invalid。
SCHEMA_BROKEN_ANGLES = '{"angles": {"claim": "把对象当成了数组"}}'

BASE_PROMPT = "你是这个内容 Agent 的观点策划。"


class QueueLLMService:
    """按调用次序出牌的假 LLM：值是字符串就返回，是异常实例就抛。

    脚本用完还再被调用时直接断言失败，而不是兜底返回点什么——"多调了一次"
    恰恰是这里最该被发现的 bug。
    """

    def __init__(self, *values: object):
        self._values = list(values)
        self.calls: list[str] = []

    async def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        self.calls.append(prompt)
        assert self._values, f"脚本已用完，第 {len(self.calls)} 次调用没有返回值"
        value = self._values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]


def _iterations() -> int:
    """该重试几次。测试不写死 2，免得改了上限而断言还停在旧值上。"""
    return MAX_STRUCTURED_OUTPUT_ATTEMPTS - 1


# ---------------------------------------------------------------------------
# 1 + 8：坏一次、重写就好
# ---------------------------------------------------------------------------


async def test_json_syntax_error_is_retried_once_and_succeeds():
    """第一次非法 JSON，第二次合法 → 成功，且恰好调用 2 次。"""
    llm = QueueLLMService(REAL_MALFORMED_ANGLES, VALID_ANGLES)

    parsed = await complete_json(llm, BASE_PROMPT, AngleDraftList)

    assert isinstance(parsed, AngleDraftList)
    assert parsed.angles[0].claim == "常驻智能体会产生没人复核的改动。"
    assert len(llm.calls) == 2


async def test_the_real_run_9876ea27_payload_now_recovers():
    """回归本次真实故障：那次死掉的输入，现在能自己走出来。

    第一次返回的就是 run `9876ea27` 里那段裸引号 JSON（当时直接终结了整个 run），
    第二次模型写对了。
    """
    llm = QueueLLMService(REAL_MALFORMED_ANGLES, VALID_ANGLES)

    parsed = await complete_json(llm, BASE_PROMPT, AngleDraftList)

    assert parsed.angles[0].evidence_ids == ["f1"]
    assert len(llm.calls) == 2


# ---------------------------------------------------------------------------
# 2：两次都坏 → 明确失败，不出现第三次
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "kind"),
    [
        (REAL_MALFORMED_ANGLES, "json_invalid"),
        (SCHEMA_BROKEN_ANGLES, "schema_invalid"),
    ],
)
async def test_two_bad_outputs_fail_with_a_classified_error(bad, kind):
    """最终失败的信息里必须看得出是哪一类坏，而不是笼统一句 invalid。"""
    llm = QueueLLMService(bad, bad)

    with pytest.raises(LLMError) as excinfo:
        await complete_json(llm, BASE_PROMPT, AngleDraftList)

    assert kind in str(excinfo.value)
    progress = f"attempt {MAX_STRUCTURED_OUTPUT_ATTEMPTS}"
    assert f"{progress}/{MAX_STRUCTURED_OUTPUT_ATTEMPTS}" in str(excinfo.value)
    # 恰好用满上限，没有第三次
    assert len(llm.calls) == MAX_STRUCTURED_OUTPUT_ATTEMPTS


async def test_retry_never_loops_beyond_the_cap():
    """连喂 5 条坏输出也只吃掉 2 条——上限是硬的，不是"直到成功为止"。"""
    llm = QueueLLMService(*[REAL_MALFORMED_ANGLES] * 5)

    with pytest.raises(LLMError):
        await complete_json(llm, BASE_PROMPT, AngleDraftList)

    assert len(llm.calls) == MAX_STRUCTURED_OUTPUT_ATTEMPTS
    assert len(llm._values) == 5 - MAX_STRUCTURED_OUTPUT_ATTEMPTS  # 还剩着，没被吃掉


# ---------------------------------------------------------------------------
# 3：重试 prompt
# ---------------------------------------------------------------------------


async def test_retry_prompt_asks_for_a_fix_without_feedback_of_the_raw_output():
    """第二次请求 = 原 prompt + 修复提示；**不含**上一次的原始输出。

    回灌原始输出既是把一段已知会诱发同样错误的样本再喂一遍，也要多花一次输入
    token。模型需要知道的只是"哪里不合规"。
    """
    llm = QueueLLMService(REAL_MALFORMED_ANGLES, VALID_ANGLES)

    await complete_json(llm, BASE_PROMPT, AngleDraftList)

    first, second = llm.calls
    assert second.startswith(first)  # 原 prompt 原样保留
    assert second != first  # 但确实追加了东西
    assert "合法 JSON" in second  # 修复提示在
    assert RAW_ONLY_FRAGMENT not in second  # 没回灌坏输出
    assert REAL_MALFORMED_ANGLES not in second
    assert "input_value" not in second  # 也没回灌 pydantic 的展示串


async def test_schema_failure_gets_the_schema_hint_not_the_quote_hint():
    """两类失败说两句话：引号转义的提示对"字段类型不对"没有意义。"""
    llm = QueueLLMService(SCHEMA_BROKEN_ANGLES, VALID_ANGLES)

    await complete_json(llm, BASE_PROMPT, AngleDraftList)

    retry_prompt = llm.calls[1]
    assert "JSON 结构" in retry_prompt
    assert "双引号" not in retry_prompt  # 引号那套不该出现在这里


# ---------------------------------------------------------------------------
# 4：schema 失败同样重试一次
# ---------------------------------------------------------------------------


async def test_schema_invalid_output_is_also_retried_once():
    llm = QueueLLMService(SCHEMA_BROKEN_ANGLES, VALID_ANGLES)

    parsed = await complete_json(llm, BASE_PROMPT, AngleDraftList)

    assert isinstance(parsed, AngleDraftList)
    assert len(llm.calls) == 2


# ---------------------------------------------------------------------------
# 5：输出被上限截断 —— 不重试
#
# 用真的 `OpenAILLMService` + 假客户端，而不是手搓一个抛异常的假服务：
# 要证明的是"llm.py 抛出来的那条路径确实一次都不重试"，那条路径只有真的走一遍才算数。
# ---------------------------------------------------------------------------


def _truncating_client(content: str, finish_reason: str):
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason=finish_reason,
                )
            ]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client, calls


async def test_truncated_output_is_not_retried():
    """`finish_reason == "length"` 保持原行为：报截断、**不增加调用次数**。

    重试同一份 prompt 必然再次撞上同一个上限——那只是把一次故障变成两次调用。
    """
    client, calls = _truncating_client('{"angles": [', finish_reason="length")
    llm = OpenAILLMService(api_key="k", client=client)

    with pytest.raises(LLMError, match="truncated by the output limit"):
        await complete_json(llm, BASE_PROMPT, AngleDraftList)

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 6：基础设施故障 —— 不重试，也不被包装成校验失败
# ---------------------------------------------------------------------------


async def test_infrastructure_error_passes_straight_through():
    """网络 / 限流 / provider 故障照旧走 LLMError，不重试、不改写类型。"""
    boom = LLMError("LLM call failed: APIConnectionError")
    llm = QueueLLMService(boom)

    with pytest.raises(LLMError, match="APIConnectionError"):
        await complete_json(llm, BASE_PROMPT, AngleDraftList)

    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# 7：一次就对 —— 不产生额外调用
# ---------------------------------------------------------------------------


async def test_valid_output_costs_exactly_one_call():
    llm = QueueLLMService(VALID_ANGLES)

    parsed = await complete_json(llm, BASE_PROMPT, AngleDraftList)

    assert isinstance(parsed, AngleDraftList)
    assert len(llm.calls) == 1


async def test_retry_is_only_for_structured_output_not_for_a_clean_run():
    """上限常量本身：一次重试，不是无限。改这个数就该有人先想清楚。"""
    assert MAX_STRUCTURED_OUTPUT_ATTEMPTS == 2
    assert _iterations() == 1


# ---------------------------------------------------------------------------
# 不改坏输出
# ---------------------------------------------------------------------------


class _Tiny(BaseModel):
    value: int = 0


@pytest.mark.parametrize(
    "repair_attempt",
    [
        '{"value": 1',  # 少一个右括号
        '```json\n{"value": 1}\n```',  # markdown 围栏
        '{"value": 1}  多余的话',  # 前后缀
    ],
)
async def test_malformed_output_is_never_repaired_in_place(repair_attempt):
    """差一点点也不算过：不补括号、不 strip 围栏、不截取前缀。

    修出来的 JSON 未必是模型想说的意思，而且会把"模型有多常在写坏 JSON"这个
    真实故障率藏起来。坏了就让它完整重写。
    """
    llm = QueueLLMService(repair_attempt, repair_attempt)

    with pytest.raises(LLMError):
        await complete_json(llm, BASE_PROMPT, _Tiny)

    assert len(llm.calls) == 2
