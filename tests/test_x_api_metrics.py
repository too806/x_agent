"""Phase 5B.4：按 id 读我们自己帖子的公开指标。

两条路径分开测：
- 客户端层（RealXApiClient + MockTransport）：请求形状、认证、错误分类。
- 语义层（XService + FakeXApiClient）：映射口径——**缺席 ≠ 0**。
"""

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.services.x_api import (
    MAX_LOOKUP_IDS,
    RealXApiClient,
    XApiResponseError,
    XApiTransportError,
)
from app.services.x_service import XService

from tests.fakes import FakeXApiClient


def _make(handler):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RealXApiClient(
        bearer_token="test-token",
        base_url="https://api.x.com",
        http_client=http_client,
    )
    return client, http_client


# --------------------------------------------------------------------------
# 客户端层
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_uses_by_id_path_bearer_and_public_metrics():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": []})

    client, _ = _make(handler)
    await client.get_posts_by_ids(["111", "222"])

    assert seen["method"] == "GET"
    assert seen["url"].startswith("https://api.x.com/2/tweets?")
    assert "ids=111%2C222" in seen["url"]
    # 只要 public_metrics：created_at 会与 published_post.published_at
    # 形成第二个来源，刻意不请求。
    assert "public_metrics" in seen["url"]
    assert "created_at" not in seen["url"]
    assert seen["auth"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_lookup_rejects_more_than_the_id_cap_without_calling_x():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    client, _ = _make(handler)
    with pytest.raises(ValueError):
        await client.get_posts_by_ids([str(i) for i in range(MAX_LOOKUP_IDS + 1)])
    # 超限必须在发请求之前就拒绝——X 会以 400 回敬，而那时额度已经花了。
    assert calls == []


@pytest.mark.asyncio
async def test_lookup_non_2xx_is_a_response_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"title": "Forbidden"})

    client, _ = _make(handler)
    with pytest.raises(XApiResponseError) as exc:
        await client.get_posts_by_ids(["111"])
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_lookup_transport_failure_is_a_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, _ = _make(handler)
    with pytest.raises(XApiTransportError):
        await client.get_posts_by_ids(["111"])


# --------------------------------------------------------------------------
# 语义层
# --------------------------------------------------------------------------


def _service(raw):
    return XService(
        FakeXApiClient(lookup_raw=raw),
        clock=lambda: datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_missing_metric_keys_stay_none_not_zero():
    """X 少给一个键 → None。这是本阶段最核心的口径。

    抄 `_map` 的 `metrics.get("like_count", 0)` 会把"没读到"记成"没人点赞"。
    """
    raw = {"data": [{"id": "111", "public_metrics": {"like_count": 7}}]}
    observations, missing = await _service(raw).fetch_public_metrics(["111"])

    assert missing == []
    obs = observations[0]
    assert obs.likes == 7
    # 其余三个没出现在响应里 → 必须是 None
    assert obs.replies is None
    assert obs.reposts is None
    assert obs.quote_count is None
    assert obs.impressions is None
    assert obs.bookmarks is None


@pytest.mark.asyncio
async def test_impressions_and_bookmarks_are_ignored_even_when_x_returns_them():
    """X **会**把这两项放进响应（真机上就是 0），v1 仍然不读。

    真机验证发现的缺陷：原来的映射写的是 `metrics.get("impression_count")`，
    并且注释断言"v1 从未请求这两项，所以恒为 None"。实际响应对
    `GET /2/tweets?ids=&tweet.fields=public_metrics` 照样回 `impression_count: 0`，
    于是快照里落下 0，而 `report.py` 只在 impressions 全为 None 时才声明
    "曝光量为未知，因此互动率无法计算（不是零互动率）"——落 0 会让这句最该说的话
    消失。工作单第十节：未知不能变成 0。
    """
    raw = {
        "data": [
            {
                "id": "111",
                "public_metrics": {
                    "like_count": 5,
                    "reply_count": 1,
                    "retweet_count": 2,
                    "quote_count": 0,
                    # 真机响应里确实有这两项，且是 0
                    "impression_count": 0,
                    "bookmark_count": 0,
                },
            }
        ]
    }
    observations, _ = await _service(raw).fetch_public_metrics(["111"])

    obs = observations[0]
    # 公开计数照读
    assert (obs.likes, obs.replies, obs.reposts, obs.quote_count) == (5, 1, 2, 0)
    # 这两项即使响应里有、即使是 0，也必须落成 None
    assert obs.impressions is None
    assert obs.bookmarks is None


@pytest.mark.asyncio
async def test_a_zero_impression_count_does_not_silence_the_unknown_impressions_note():
    """端到端的一句：快照里没有 0 曝光 → 报告才会如实说"互动率算不出来"。"""
    from app.feedback.profile import observations_from_rows
    from app.feedback.report import build_report

    class _Row:
        id = "snap-1"
        x_post_id = "111"
        published_post_id = "post-1"
        observed_at = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)
        hours_since_publish = 0.05
        likes, replies, reposts, quotes = 5, 1, 2, 0
        impressions = None
        bookmarks = None

    report = build_report(observations_from_rows([_Row()]))
    assert any("曝光量为未知" in note for note in report.limitations)


@pytest.mark.asyncio
async def test_ids_x_did_not_return_are_reported_not_fabricated():
    raw = {"data": [{"id": "111", "public_metrics": {"like_count": 3}}]}
    observations, missing = await _service(raw).fetch_public_metrics(["111", "999"])

    assert [o.platform_post_id for o in observations] == ["111"]
    assert missing == ["999"]  # 如实交出，绝不补一条全 0


@pytest.mark.asyncio
async def test_observed_at_is_one_shared_stamp_per_fetch():
    raw = {
        "data": [
            {"id": "111", "public_metrics": {"like_count": 1}},
            {"id": "222", "public_metrics": {"like_count": 2}},
        ]
    }
    observations, _ = await _service(raw).fetch_public_metrics(["111", "222"])
    stamps = {o.observed_at for o in observations}
    assert len(stamps) == 1  # 一次抓取 = 一次观察


@pytest.mark.asyncio
async def test_explicit_observed_at_overrides_the_clock():
    raw = {"data": [{"id": "111", "public_metrics": {}}]}
    pinned = datetime(2026, 1, 1, tzinfo=timezone.utc)
    observations, _ = await _service(raw).fetch_public_metrics(
        ["111"], observed_at=pinned
    )
    assert observations[0].observed_at == pinned


@pytest.mark.asyncio
async def test_empty_ids_never_call_x():
    client = FakeXApiClient(lookup_raw={"data": []})
    service = XService(client)
    observations, missing = await service.fetch_public_metrics([])
    assert (observations, missing) == ([], [])
    assert client.lookups == []


@pytest.mark.asyncio
async def test_entries_without_an_id_are_skipped():
    raw = {"data": [{"public_metrics": {"like_count": 1}}, {"id": "111"}]}
    observations, missing = await _service(raw).fetch_public_metrics(["111"])
    assert [o.platform_post_id for o in observations] == ["111"]
    assert missing == []
