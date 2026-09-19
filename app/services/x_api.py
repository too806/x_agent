from typing import Protocol

import httpx

from app.config import settings


class XApiError(Exception):
    """X API 调用失败（网络错误或非 2xx 响应）。"""


class XApiClient(Protocol):
    async def search_recent_tweets(self, query: str, max_results: int = 10) -> dict:
        """返回 /2/tweets/search/recent 的原始 JSON dict（data/includes/meta）。"""


class RealXApiClient:
    """基于官方 X API v2 的搜索客户端（app-only Bearer token）。

    字段映射依据官方 v2 Tweet/User 对象：id / text / author_id / created_at /
    public_metrics{retweet_count, reply_count, like_count, quote_count}。
    """

    def __init__(
        self,
        bearer_token: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._bearer_token = bearer_token or settings.x_api_bearer_token
        self._base_url = (base_url or settings.x_api_base_url).rstrip("/")
        self._timeout = timeout
        self._http_client = http_client

    async def search_recent_tweets(self, query: str, max_results: int = 10) -> dict:
        url = f"{self._base_url}/2/tweets/search/recent"
        params = {
            "query": query,
            "max_results": max_results,
            "tweet.fields": "author_id,created_at,public_metrics",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        headers = {"Authorization": f"Bearer {self._bearer_token}"}

        client = self._http_client
        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                resp = await client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise XApiError(f"X API request failed: {exc}") from exc
            if resp.status_code >= 400:
                raise XApiError(f"X API returned {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        finally:
            if should_close:
                await client.aclose()
