from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.llm import LLMError, OpenAILLMService


def _fake_client(content="ok", error=None, finish_reason="stop"):
    """假客户端。

    `finish_reason` 是照抄 OpenAI SDK 的 `Choice` 形状而不是省略它：真机上
    这个字段永远存在，假对象少了它，业务代码就只能写成 `getattr(...,None)`，
    而那个写法会让"SDK 换了字段名"变成静默失效——正好是这段守卫要防的事。
    """
    calls = {}

    async def create(**kwargs):
        calls["kwargs"] = kwargs
        if error is not None:
            raise error
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason=finish_reason,
                )
            ]
        )

    completions = SimpleNamespace(create=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, calls


async def test_complete_returns_content():
    client, calls = _fake_client(content="hello")
    svc = OpenAILLMService(api_key="k", client=client)
    out = await svc.complete("prompt")
    assert out == "hello"
    assert calls["kwargs"]["model"] == settings.llm_model
    assert calls["kwargs"]["messages"] == [{"role": "user", "content": "prompt"}]


async def test_complete_json_mode_sets_response_format():
    client, calls = _fake_client(content='{"a": 1}')
    svc = OpenAILLMService(api_key="k", client=client)
    await svc.complete("prompt", json_mode=True)
    assert calls["kwargs"]["response_format"] == {"type": "json_object"}


async def test_complete_reports_truncation_instead_of_returning_a_prefix():
    """被 max_tokens 截断的输出是**合法前缀**，直接返回等于把错误往下游挪。

    真机 smoke 里就是这样：证据那一步报 `invalid EvidencePack: Invalid JSON:
    expected , or ] at line 1 column 5959`，而真实原因是模型的话没说完。
    这里钉住的是"错误必须在发生的那一层说出来"，因为两者的处置不同——
    一个是调 prompt，一个是调上限。
    """
    client, _ = _fake_client(content='{"facts": [', finish_reason="length")
    svc = OpenAILLMService(api_key="k", client=client)
    with pytest.raises(LLMError, match="truncated by the output limit"):
        await svc.complete("prompt", json_mode=True)


async def test_complete_raises_on_sdk_error():
    client, _ = _fake_client(error=RuntimeError("sdk boom"))
    svc = OpenAILLMService(api_key="k", client=client)
    with pytest.raises(LLMError):
        await svc.complete("prompt")


async def test_complete_requires_api_key():
    svc = OpenAILLMService(api_key="", client=None)
    with pytest.raises(LLMError):
        await svc.complete("prompt")
