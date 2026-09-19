import json

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
    def __init__(self, raw=None, error=None):
        self._raw = raw if raw is not None else SAMPLE_X_RESPONSE
        self._error = error

    async def search_recent_tweets(self, query: str, max_results: int = 10) -> dict:
        if self._error is not None:
            raise self._error
        return self._raw


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


def draft_json(
    content: str = "AI coding agents ship faster.",
    hook_type: str = "statement",
    topic: str = "AI coding agent",
) -> str:
    return json.dumps({"content": content, "hook_type": hook_type, "topic": topic})
