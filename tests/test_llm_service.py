from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.llm import LLMError, OpenAILLMService


def _fake_client(content="ok", error=None):
    calls = {}

    async def create(**kwargs):
        calls["kwargs"] = kwargs
        if error is not None:
            raise error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
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


async def test_complete_raises_on_sdk_error():
    client, _ = _fake_client(error=RuntimeError("sdk boom"))
    svc = OpenAILLMService(api_key="k", client=client)
    with pytest.raises(LLMError):
        await svc.complete("prompt")


async def test_complete_requires_api_key():
    svc = OpenAILLMService(api_key="", client=None)
    with pytest.raises(LLMError):
        await svc.complete("prompt")
