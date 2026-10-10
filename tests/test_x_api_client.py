import json
import re
from datetime import datetime, timezone

import httpx
import pytest

from app.services.x_api import (
    END_TIME_MIN_LAG,
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


def _header_without_signature(header: str) -> str:
    """去掉 `oauth_signature` 那个参数之后的头文本。

    "凭据有没有泄漏进请求头"这个检查必须先把签名摘出去再搜：签名是 HMAC-SHA1
    的 base64（字符集含大小写字母与数字），里面**偶然**拼出凭据的那两个字符，
    与"凭据被写进了头里"是两件毫不相干的事。原来的断言搜整条头，于是它按
    nonce/时间随机抖动——`oauth_nonce` 是 `secrets.token_hex(16)`（十六进制，
    不含 's'），抖动只可能来自签名；实测失败那次的头里就是
    `oauth_signature="yj9deYHcsWcUPhP7tSj8yXBli48%3D"` 恰好含 "cs"。

    摘掉签名后，剩下的全是**真实参数值**（consumer key / token / nonce /
    timestamp / method / version），凭据一旦真被写进其中任何一个，检查照样会响。
    """
    return re.sub(r'oauth_signature="[^"]*"', "", header)


def test_the_credential_leak_check_ignores_the_random_signature():
    """回归：签名里偶然出现的凭据子串不算泄漏，真泄漏仍然会被抓到。

    这条不依赖运气——它直接喂一个**必然**含 "cs" 的签名，钉住"先摘签名"这一步
    不会被改回"搜整条头"；反例那一半保证摘签名没有把检查摘成摆设。
    """
    header = (
        'OAuth oauth_consumer_key="ck", oauth_nonce="0011", '
        'oauth_signature="ycs%2Bdeadbeef", oauth_token="tk"'
    )

    # 签名含 "cs" → 不算泄漏（这正是那个间歇性失败要修的地方）
    assert "cs" not in _header_without_signature(header).replace("ck", "")

    # 签名之外的参数里出现凭据 → 必须响
    leaked = header.replace('oauth_nonce="0011"', 'oauth_nonce="cs"')
    assert "cs" in _header_without_signature(leaked).replace("ck", "")


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
    assert "cs" not in _header_without_signature(seen["auth"]).replace("ck", "")


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


# ---------------------------------------------------------------------------
# 窗口右端：X 不接受"离现在不到 10 秒"的 end_time
# ---------------------------------------------------------------------------


def _parse_end_time(request) -> datetime:
    raw = dict(request.url.params)["end_time"]
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


async def test_search_pushes_end_time_back_from_now():
    """`end_time = now` 会被 X 判成参数非法——这是真机上 4/5 次失败的原因。

    离线测试的 fake 客户端不检查这条规则，所以这个缺陷只有在真机 smoke run
    里才暴露得出来；正因为如此，这里必须把修正后的行为钉住：
    发出去的 end_time 至少要比本机当前时刻早 `END_TIME_MIN_LAG`。
    """

    def handler(request):
        assert _parse_end_time(request) <= datetime.now(timezone.utc) - END_TIME_MIN_LAG
        return httpx.Response(200, json={"data": [], "meta": {"result_count": 0}})

    client, http_client = _make(handler)
    try:
        await client.search_recent_tweets("ai", end_time=datetime.now(timezone.utc))
    finally:
        await http_client.aclose()


async def test_search_does_not_move_an_older_end_time_forward():
    """已经在过去很久的右端原样送出。

    收窄只发生在"太靠近现在"这一侧：如果无条件改成 now − lag，
    一次针对历史窗口的扫描会被悄悄拉到最近，而那是最难发现的一类错误——
    回来的数据看着正常，只是不是被问到的那一段。
    """
    old = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    seen: list[datetime] = []

    def handler(request):
        seen.append(_parse_end_time(request))
        return httpx.Response(200, json={"data": [], "meta": {"result_count": 0}})

    client, http_client = _make(handler)
    try:
        await client.search_recent_tweets("ai", end_time=old)
        assert seen == [old]
    finally:
        await http_client.aclose()


async def test_search_without_end_time_sends_no_window():
    """不传右端时**不要**自作主张补一个：那是调用方在说"我不限定右端"。"""

    def handler(request):
        assert "end_time" not in dict(request.url.params)
        return httpx.Response(200, json={"data": [], "meta": {"result_count": 0}})

    client, http_client = _make(handler)
    try:
        await client.search_recent_tweets("ai")
    finally:
        await http_client.aclose()
