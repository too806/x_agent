from typing import Protocol

from openai import AsyncOpenAI

from app.config import settings


class LLMError(Exception):
    """LLM 调用失败。"""


class LLMService(Protocol):
    async def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        """执行一次文本补全，返回文本内容（json_mode=True 时要求返回合法 JSON）。"""


class OpenAILLMService:
    """基于 OpenAI 兼容接口的 LLM 封装（base_url 可指向任意兼容服务）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        client: AsyncOpenAI | None = None,
    ):
        self._api_key = api_key if api_key is not None else settings.llm_api_key
        self._base_url = base_url or settings.llm_base_url
        self._model = model or settings.llm_model
        self._timeout = timeout if timeout is not None else settings.llm_timeout_seconds
        self._client = client
        self._injected_client = client

    def _get_client(self) -> AsyncOpenAI:
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        if not self._api_key:
            raise LLMError("LLM_API_KEY is not configured")
        kwargs: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._get_client().chat.completions.create(**kwargs)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001  # 包装 SDK/网络错误，不泄露 key
            raise LLMError(f"LLM call failed: {exc.__class__.__name__}") from exc
        content = resp.choices[0].message.content
        if content is None:
            raise LLMError("LLM returned empty content")
        return content
