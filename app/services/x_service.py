from datetime import datetime

from app.schemas import ResearchResult
from app.services.x_api import XApiClient


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class XService:
    """面向业务的研究封装：调用 XApiClient，把原始响应映射为 ResearchResult。"""

    def __init__(self, client: XApiClient):
        self._client = client

    async def research(self, topic: str, max_results: int = 10) -> list[ResearchResult]:
        raw = await self._client.search_recent_tweets(topic, max_results)
        return self._map(raw)

    def _map(self, raw: dict) -> list[ResearchResult]:
        data = raw.get("data") or []
        users = {u["id"]: u for u in (raw.get("includes") or {}).get("users", [])}
        results: list[ResearchResult] = []
        for t in data:
            author = users.get(t.get("author_id"))
            username = author.get("username") if author else None
            metrics = t.get("public_metrics") or {}
            post_id = t.get("id")
            url = f"https://x.com/{username}/status/{post_id}" if username and post_id else None
            results.append(
                ResearchResult(
                    platform_post_id=post_id or "",
                    author_username=username,
                    content=t.get("text", ""),
                    url=url,
                    likes=metrics.get("like_count", 0),
                    reposts=metrics.get("retweet_count", 0),
                    replies=metrics.get("reply_count", 0),
                    published_at=_parse_iso(t.get("created_at")),
                )
            )
        return results
