import httpx
import pytest

from app.services.x_api import RealXApiClient, XApiError


def _make(handler):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RealXApiClient(
        bearer_token="test-token",
        base_url="https://api.x.com",
        http_client=http_client,
    )
    return client, http_client


async def test_search_recent_returns_json():
    def handler(request):
        assert request.url.path == "/2/tweets/search/recent"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert "tweet.fields" in request.url.query.decode()
        return httpx.Response(200, json={"data": [], "meta": {"result_count": 0}})

    client, http_client = _make(handler)
    try:
        resp = await client.search_recent_tweets("ai")
        assert resp["meta"]["result_count"] == 0
    finally:
        await http_client.aclose()


async def test_search_raises_on_http_error():
    def handler(request):
        return httpx.Response(429, json={"title": "Too Many Requests"})

    client, http_client = _make(handler)
    try:
        with pytest.raises(XApiError):
            await client.search_recent_tweets("ai")
    finally:
        await http_client.aclose()


async def test_search_raises_on_network_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client, http_client = _make(handler)
    try:
        with pytest.raises(XApiError):
            await client.search_recent_tweets("ai")
    finally:
        await http_client.aclose()


async def test_search_raises_on_invalid_json():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    client, http_client = _make(handler)
    try:
        with pytest.raises(XApiError):
            await client.search_recent_tweets("ai")
    finally:
        await http_client.aclose()
