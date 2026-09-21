import json

import httpx
import pytest

from app.services.x_api import (
    RealXApiClient,
    XApiConfigError,
    XApiError,
    XApiResponseError,
    XApiTransportError,
)

OAUTH_CREDS = {
    "consumer_key": "ck",
    "consumer_secret": "cs",
    "access_token": "tk",
    "access_token_secret": "ts",
}


def _make(handler):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RealXApiClient(
        bearer_token="test-token",
        base_url="https://api.x.com",
        http_client=http_client,
    )
    return client, http_client


def _make_publisher(handler):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RealXApiClient(
        base_url="https://api.x.com", http_client=http_client, **OAUTH_CREDS
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


async def test_search_error_carries_status_code():
    """status_code 是发布路径分类 4xx/5xx 的依据，必须带出来。"""

    def handler(request):
        return httpx.Response(429, json={"title": "Too Many Requests"})

    client, http_client = _make(handler)
    try:
        with pytest.raises(XApiResponseError) as excinfo:
            await client.search_recent_tweets("ai")
        assert excinfo.value.status_code == 429
    finally:
        await http_client.aclose()


async def test_search_connect_error_is_marked_as_not_delivered():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client, http_client = _make(handler)
    try:
        with pytest.raises(XApiTransportError) as excinfo:
            await client.search_recent_tweets("ai")
        assert excinfo.value.may_have_reached_x is False
    finally:
        await http_client.aclose()


# ---------- create_post（OAuth 1.0a 写路径）----------


async def test_create_post_sends_oauth_header_and_json_body():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["content_type"] = request.headers["Content-Type"]
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"data": {"id": "1900000000000000001", "text": "hi"}})

    client, http_client = _make_publisher(handler)
    try:
        post_id = await client.create_post("hi")
    finally:
        await http_client.aclose()

    assert post_id == "1900000000000000001"
    assert seen["path"] == "/2/tweets"
    # 必须走 OAuth 1.0a user context，绝不能退化成 Bearer
    assert seen["auth"].startswith("OAuth ")
    assert "Bearer" not in seen["auth"]
    assert 'oauth_signature_method="HMAC-SHA1"' in seen["auth"]
    assert "oauth_signature=" in seen["auth"]
    assert seen["content_type"] == "application/json"
    assert json.loads(seen["body"]) == {"text": "hi"}
    # 凭据本身绝不能出现在请求头里（只出现签名）
    assert "cs" not in seen["auth"].replace("ck", "")


async def test_create_post_requires_all_four_credentials():
    """凭据缺失必须在发出任何请求之前失败，且不能是 XApiError。"""

    def handler(request):  # pragma: no cover - 不应该被调用
        raise AssertionError("must not send a request without credentials")

    client, http_client = _make_publisher(handler)
    client._access_token_secret = ""  # noqa: SLF001 - 模拟 .env 缺一项
    try:
        with pytest.raises(XApiConfigError) as excinfo:
            await client.create_post("hi")
        assert "X_ACCESS_TOKEN_SECRET" in str(excinfo.value)
        # 不是 XApiError → HTTP 层会映射成 500 而不是 502（这是我们的配置错误）
        assert not isinstance(excinfo.value, XApiError)
    finally:
        await http_client.aclose()


async def test_create_post_402_is_a_definite_rejection():
    """402 credits depleted：X 明确拒绝，可以确定没有创建内容。"""

    def handler(request):
        return httpx.Response(
            402,
            json={
                "detail": "credits depleted",
                "status": 402,
                "title": "Payment Required",
            },
        )

    client, http_client = _make_publisher(handler)
    try:
        with pytest.raises(XApiResponseError) as excinfo:
            await client.create_post("hi")
        assert excinfo.value.status_code == 402
        assert 400 <= excinfo.value.status_code < 500
    finally:
        await http_client.aclose()


async def test_create_post_500_is_not_a_decision():
    def handler(request):
        return httpx.Response(503, json={"title": "Service Unavailable"})

    client, http_client = _make_publisher(handler)
    try:
        with pytest.raises(XApiResponseError) as excinfo:
            await client.create_post("hi")
        # 5xx 必须落在 4xx 之外：调用方据此保持"结果未知"
        assert not (400 <= excinfo.value.status_code < 500)
    finally:
        await http_client.aclose()


async def test_create_post_read_timeout_marks_result_as_unknown():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client, http_client = _make_publisher(handler)
    try:
        with pytest.raises(XApiTransportError) as excinfo:
            await client.create_post("hi")
        # 请求可能已经送达并被处理 → 绝不能判"没发出去"
        assert excinfo.value.may_have_reached_x is True
    finally:
        await http_client.aclose()


async def test_create_post_connect_timeout_marks_result_as_not_delivered():
    def handler(request):
        raise httpx.ConnectTimeout("connect timed out", request=request)

    client, http_client = _make_publisher(handler)
    try:
        with pytest.raises(XApiTransportError) as excinfo:
            await client.create_post("hi")
        assert excinfo.value.may_have_reached_x is False
    finally:
        await http_client.aclose()


async def test_create_post_2xx_without_id_is_indeterminate():
    """2xx 但拿不到 id：请求确实处理过了，结果却无法判定。

    必须仍然带 2xx 的 status_code，调用方才不会把它错判成 4xx（"确定没发出去"）。
    """

    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    client, http_client = _make_publisher(handler)
    try:
        with pytest.raises(XApiResponseError) as excinfo:
            await client.create_post("hi")
        assert excinfo.value.status_code == 200
        assert not (400 <= excinfo.value.status_code < 500)
    finally:
        await http_client.aclose()
