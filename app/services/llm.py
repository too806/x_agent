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

    @property
    def model_name(self) -> str:
        """本次实际使用的模型名。

        公开出来是为了**留痕**：`style_sample.annotation_model` 要记的是
        "这个标注是哪版模型给的"，而重跑标注会静默改变 Profile。
        没有它，那一列只能记成类名（`OpenAILLMService`），
        而"换了模型重标一遍"与"同一模型重标一遍"就分不开了。
        """
        return self._model

    def _get_client(self) -> AsyncOpenAI:
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
        choice = resp.choices[0]
        content = choice.message.content
        if content is None:
            raise LLMError("LLM returned empty content")
        # 截断必须在这里就要说出来。被截断的输出是一段**合法前缀**，
        # 下一层看到的只会是"JSON 语法错误，第 1 行第 5959 列"——真机 smoke 里
        # 就是这样：证据那一步报 invalid EvidencePack，而真实原因是模型的话
        # 没说完。两者的处置完全不同（一个是调 prompt，一个是调上限），
        # 所以这个判断不能留给读日志的人去猜。
        if choice.finish_reason == "length":
            # 措辞刻意不提某个配置项：本仓库**没有**设置 max_tokens（走服务端默认），
            # 所以这里给不出"把 X 调大"这种能照着做的指示。说清楚的只有两件事——
            # 输出是被上限截断的，以及要修就得少要一点内容。
            raise LLMError(
                f"LLM output was truncated by the output limit (chars={len(content)}); "
                "the response is incomplete, not malformed"
            )
        return content
