import pytest

from app.schemas import ResearchResult
from app.services.x_api import XApiError
from app.services.x_service import XService
from fakes import FakeXApiClient


async def test_research_maps_fields():
    service = XService(FakeXApiClient())
    results = await service.research("ai")

    assert len(results) == 2
    r = results[0]
    assert isinstance(r, ResearchResult)
    assert r.platform_post_id == "1001"
    assert r.author_username == "ada_dev"
    assert r.content == "AI coding agents are transforming how teams ship software."
    assert r.url == "https://x.com/ada_dev/status/1001"
    assert r.likes == 42
    assert r.reposts == 5
    assert r.replies == 3
    assert r.published_at is not None


async def test_research_handles_empty_data():
    service = XService(FakeXApiClient(raw={"data": [], "meta": {"result_count": 0}}))
    assert await service.research("ai") == []


async def test_research_propagates_error():
    service = XService(FakeXApiClient(error=XApiError("boom")))
    with pytest.raises(XApiError):
        await service.research("ai")
