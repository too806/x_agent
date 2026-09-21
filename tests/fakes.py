import json
from types import SimpleNamespace

SAMPLE_X_RESPONSE = {
    "data": [
        {
            "id": "1001",
            "text": "AI coding agents are transforming how teams ship software.",
            "author_id": "u1",
            "created_at": "2026-09-18T10:00:00.000Z",
            "public_metrics": {
                "retweet_count": 5,
                "reply_count": 3,
                "like_count": 42,
                "quote_count": 1,
            },
        },
        {
            "id": "1002",
            "text": "Here is how we use agents for code review.",
            "author_id": "u2",
            "created_at": "2026-09-17T08:30:00.000Z",
            "public_metrics": {
                "retweet_count": 2,
                "reply_count": 1,
                "like_count": 10,
                "quote_count": 0,
            },
        },
    ],
    "includes": {
        "users": [
            {"id": "u1", "name": "Ada", "username": "ada_dev"},
            {"id": "u2", "name": "Bob", "username": "bob_codes"},
        ]
    },
    "meta": {"result_count": 2, "newest_id": "1001", "oldest_id": "1002"},
}


class FakeXApiClient:
    """假的 HTTP 客户端层：读（search）与写（create_post）都覆盖。

    publish 的写路径错误走 `post_error` 单独注入，避免一处出错两种能力全废。
    `created` 记录每次发布的正文——发布测试必须精确断言调用次数。
    """

    def __init__(
        self,
        raw=None,
        error=None,
        post_id: str = "1900000000000000001",
        post_error: BaseException | None = None,
    ):
        self._raw = raw if raw is not None else SAMPLE_X_RESPONSE
        self._error = error
        self._post_id = post_id
        self._post_error = post_error
        self.created: list[str] = []

    async def search_recent_tweets(self, query: str, max_results: int = 10) -> dict:
        if self._error is not None:
            raise self._error
        return self._raw

    async def create_post(self, text: str) -> str:
        self.created.append(text)
        if self._post_error is not None:
            raise self._post_error
        return self._post_id


class FakeLLMService:
    def __init__(self, text=None, error=None):
        self._text = text
        self._error = error
        self.calls: list[dict] = []

    async def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        self.calls.append({"prompt": prompt, "json_mode": json_mode})
        if self._error is not None:
            raise self._error
        return self._text


class FakePublishingXService:
    """记录发布调用的假 X 服务。

    发布相关的测试必须**精确断言调用次数**：只断言返回值/状态的测试在
    "重复发推"这类 bug 面前是恒真的——发两次的返回值和发一次一模一样。
    """

    def __init__(self, *, post_id: str = "1900000000000000001", error: BaseException | None = None):
        self._post_id = post_id
        self._error = error
        self.published: list[str] = []

    async def publish(self, content: str) -> str:
        self.published.append(content)
        if self._error is not None:
            raise self._error
        return self._post_id


class StubGraph:
    """只实现 aget_state 的 Graph 替身，用来喂 final_content 给发布服务。"""

    def __init__(self, values: dict | None = None):
        self._values = values or {}
        self.state_reads = 0

    async def aget_state(self, config):
        self.state_reads += 1
        return SimpleNamespace(values=self._values)


def draft_json(
    content: str = "AI coding agents ship faster.",
    hook_type: str = "statement",
    topic: str = "AI coding agent",
) -> str:
    return json.dumps({"content": content, "hook_type": hook_type, "topic": topic})
