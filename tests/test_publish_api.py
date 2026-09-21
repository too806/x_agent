"""发布 API 层测试。全流程走真实 HTTP + 真实 PostgreSQL，X 全部为 fake。

这里验证的是 HTTP 契约与错误映射，以及一条容易被忽略的规则：
**发布失败绝不能污染 agent_run 的流水线结论。**
"""
import asyncio
import uuid

from app.repositories import claim_publish
from app.services.x_api import XApiResponseError, XApiTransportError
from app.services.x_service import XService
from fakes import FakeXApiClient
from support import api_for, runtime_with_checkpointer

POST_ID = "1900000000000000001"


class _Client:
    """记录发布行为，方便断言"X 被调用了几次"。"""

    def __init__(self, **kwargs):
        self.api_client = FakeXApiClient(**kwargs)
        self.x_service = XService(self.api_client)

    @property
    def created(self) -> list[str]:
        return self.api_client.created


def _client(**kwargs) -> _Client:
    return _Client(**kwargs)


async def _start_run(api, topic: str = "AI coding agent") -> dict:
    resp = await api.post("/api/runs", json={"topic": topic})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "waiting_for_review"
    return resp.json()


async def _approve(api, run_id: str) -> dict:
    resp = await api.post(f"/api/runs/{run_id}/review", json={"action": "approve"})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _prepared(api) -> dict:
    started = await _start_run(api)
    await _approve(api, started["run_id"])
    return started


async def _run_detail(api, run_id: str) -> dict:
    resp = await api.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _strand_in_publishing(runtime, started: dict) -> None:
    """模拟「抢占已提交、进程随即崩溃」：库里留下 publishing，X 从未被调用。

    用 claim_publish 本身来制造残留是刻意的——它正是崩溃前最后成功执行的那一步，
    因此这样构造出来的行与真实残留逐字段一致（两张表都停在 publishing）。
    """
    async with runtime.session_factory() as session:
        claimed = await claim_publish(
            session,
            agent_run_id=started["run_id"],
            draft_id=started["draft_id"],
            body=started["draft_content"],
        )
    assert claimed is True


# ---------- 正常路径 ----------


async def test_publish_endpoint_returns_published_record(session_factory):
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            resp = await api.post(f"/api/runs/{started['run_id']}/publish")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["run_id"] == started["run_id"]
    assert data["outcome"] == "published"
    assert data["error"] is None
    assert data["publish"]["status"] == "published"
    assert data["publish"]["x_post_id"] == POST_ID
    assert data["publish"]["body"] == started["draft_content"]
    assert data["publish"]["last_error"] is None
    assert x.created == [started["draft_content"]]  # X 恰好被调用一次


async def test_get_run_exposes_publish_state_and_finished_at(session_factory):
    """发布状态能一路从 GET 看到，finished_at 的语义全程一致。

    approved 的 Run 还没结束（它等着发布），finished_at 必须为空；发布成功才是
    终态。否则"流水线跑完了"和"这个 Run 结束了"就分不开。
    """
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]

            before = await _run_detail(api, run_id)
            await api.post(f"/api/runs/{run_id}/publish")
            after = await _run_detail(api, run_id)

    assert before["publish"] is None
    assert before["finished_at"] is None  # 还没发布 → 不是终态
    assert after["status"] == "published"
    assert after["finished_at"] is not None
    assert after["publish"]["status"] == "published"
    assert after["publish"]["x_post_id"] == POST_ID
    assert after["publish"]["draft_id"] == started["draft_id"]


async def test_edit_path_publishes_edited_content(session_factory):
    edited = "AI coding agents ship much faster."
    x = _client()
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            await api.post(
                f"/api/runs/{started['run_id']}/review",
                json={"action": "edit", "content": edited},
            )
            resp = await api.post(f"/api/runs/{started['run_id']}/publish")

    assert resp.status_code == 200
    assert x.created == [edited]
    assert resp.json()["publish"]["body"] == edited


# ---------- 前置条件 ----------


async def test_publish_unknown_run_returns_404(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            resp = await api.post(f"/api/runs/{uuid.uuid4()}/publish")

    assert resp.status_code == 404


async def test_publish_before_review_returns_409(session_factory):
    x = _client()
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)  # 停在 waiting_for_review
            resp = await api.post(f"/api/runs/{started['run_id']}/publish")

    assert resp.status_code == 409
    assert x.created == []


async def test_publish_rejected_run_returns_409(session_factory):
    """被拒绝的内容绝不允许发布。"""
    x = _client()
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            await api.post(
                f"/api/runs/{started['run_id']}/review",
                json={"action": "reject", "reason": "off brand"},
            )
            resp = await api.post(f"/api/runs/{started['run_id']}/publish")

    assert resp.status_code == 409
    assert x.created == []


async def test_second_publish_returns_409_without_calling_x_again(session_factory):
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            first = await api.post(f"/api/runs/{run_id}/publish")
            second = await api.post(f"/api/runs/{run_id}/publish")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["publish"]["x_post_id"] == POST_ID  # 冲突里带回已有记录
    assert len(x.created) == 1  # 关键：X 只被调用过一次


# ---------- 失败映射 ----------


async def test_x_rejection_maps_to_502_and_stays_retryable(session_factory):
    x = _client(post_error=XApiResponseError(402, "402 credits depleted"))
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            resp = await api.post(f"/api/runs/{run_id}/publish")
            detail = await _run_detail(api, run_id)

    assert resp.status_code == 502
    assert resp.json()["outcome"] == "failed"
    assert "402" in resp.json()["publish"]["last_error"]
    # 可重试：状态是 failed 而不是 publishing
    assert detail["publish"]["status"] == "failed"


async def test_unknown_outcome_maps_to_502_and_requires_manual_review(session_factory):
    x = _client(post_error=XApiTransportError("read timed out", may_have_reached_x=True))
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            resp = await api.post(f"/api/runs/{run_id}/publish")
            detail = await _run_detail(api, run_id)

    assert resp.status_code == 502
    assert resp.json()["outcome"] == "unknown"
    # 尝试已结束但结果无法判定 → publish_unknown，诚实地表示"我们不知道发出去没有"
    assert detail["status"] == "publish_unknown"
    assert detail["publish"]["status"] == "publish_unknown"
    assert detail["publish"]["x_post_id"] is None


async def test_publishing_residue_blocks_publish_without_calling_x(session_factory):
    """残留的 publishing（进程崩溃留下的）绝不能被再次抢占。

    这是恢复场景的核心：进程在调用 X 期间挂掉，库里留下 publishing。
    重启后任何自动路径都不许再发一次，只能靠人工裁决。
    """
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            # 直接写下崩溃残留：Run 与发布行都停在 publishing
            await _strand_in_publishing(runtime, started)

            resp = await api.post(f"/api/runs/{run_id}/publish")
            detail = await _run_detail(api, run_id)

    assert resp.status_code == 409
    assert resp.json()["publish"]["status"] == "publishing"
    assert detail["status"] == "publishing"
    assert x.created == []  # 关键：一次 X 都没调


async def test_recovery_does_not_republish_after_runtime_rebuild(session_factory):
    """崩溃恢复：换一个全新的 runtime（新连接池 + 新 Graph），也不能自动重发。

    发布状态只存在 PostgreSQL 里，所以"重启"在测试里等价于换一个 runtime。
    新 runtime 拿不到任何进程内状态，只能读库——读到的必须是"不许发"。
    """
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime_a:
        async with api_for(runtime_a) as api_a:
            started = await _prepared(api_a)
            run_id = started["run_id"]
            await _strand_in_publishing(runtime_a, started)

    # —— 全新 runtime：进程内没有任何东西被复用 ——
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime_b:
        assert runtime_b.publish_service is not runtime_a.publish_service
        async with api_for(runtime_b) as api_b:
            resp = await api_b.post(f"/api/runs/{run_id}/publish")
            detail = await _run_detail(api_b, run_id)

    assert resp.status_code == 409
    assert detail["status"] == "publishing"
    assert x.created == []


async def test_manual_resolution_unblocks_the_run(session_factory):
    """人工裁决是唯一出口：裁决之后 Run 必须回到可发布状态。"""
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            await _strand_in_publishing(runtime, started)

            await api.post(
                f"/api/runs/{run_id}/publish/resolve",
                json={"action": "retry", "confirm_maybe_duplicate": True},
            )
            detail = await _run_detail(api, run_id)
            # 裁决只纠正状态，不发帖；重发要另外显式再调一次
            assert x.created == []

            resp = await api.post(f"/api/runs/{run_id}/publish")

    assert detail["status"] == "approved"
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "published"
    assert x.created == [started["draft_content"]]


async def test_publish_failure_does_not_pollute_the_run(session_factory):
    """发布失败不是流水线失败。

    agent_run 的 status / error 必须原样保留审核结论；发布的状态只记在
    published_post 上。这里也在防"发布异常被 _fail_run 兜住"那条路径——
    那会把一个已审核通过的 Run 打成 failed，并让调用方误判成审核失败。
    """
    x = _client(post_error=XApiResponseError(402, "402 credits depleted"))
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            await api.post(f"/api/runs/{run_id}/publish")
            detail = await _run_detail(api, run_id)

    assert detail["status"] == "approved"
    assert detail["error"] is None
    assert detail["final_content"] == started["draft_content"]


async def test_publish_is_not_reported_as_an_upstream_failure_of_the_run(session_factory):
    """发布失败时返回的是 published_post 的状态，不是 agent_run 被改写的状态。"""
    x = _client(post_error=XApiResponseError(503, "503 unavailable"))
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            await api.post(f"/api/runs/{run_id}/publish")
            # 结果未知时仍然可以再次请求：会被抢占挡住（409），而不是重复发推
            again = await api.post(f"/api/runs/{run_id}/publish")

    assert again.status_code == 409
    assert len(x.created) == 1


# ---------- 并发 ----------


async def test_concurrent_publish_requests_call_x_exactly_once(session_factory):
    """HTTP 层并发：三个请求同时到达，X 只能被调用一次。

    没有抢占保护时三个请求都会返回 200，而 X 上会出现三条一模一样的内容。
    """
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            responses = await asyncio.gather(
                *[api.post(f"/api/runs/{run_id}/publish") for _ in range(3)]
            )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409, 409], codes
    assert len(x.created) == 1


# ---------- 人工裁决 ----------


async def test_resolve_mark_published(session_factory):
    x = _client(post_error=XApiTransportError("read timed out", may_have_reached_x=True))
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            await api.post(f"/api/runs/{run_id}/publish")

            resp = await api.post(
                f"/api/runs/{run_id}/publish/resolve",
                json={"action": "mark_published", "x_post_id": POST_ID},
            )
            detail = await _run_detail(api, run_id)

    assert resp.status_code == 200
    # outcome 表达"裁决生效了"；结果状态在 publish.status 里
    assert resp.json()["outcome"] == "resolved"
    assert detail["publish"]["status"] == "published"
    assert detail["publish"]["x_post_id"] == POST_ID
    assert len(x.created) == 1  # 裁决本身不发帖


async def test_resolve_retry_flips_status_without_posting(session_factory):
    x = _client(post_error=XApiTransportError("read timed out", may_have_reached_x=True))
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            await api.post(f"/api/runs/{run_id}/publish")

            resp = await api.post(
                f"/api/runs/{run_id}/publish/resolve",
                json={"action": "retry", "confirm_maybe_duplicate": True},
            )

    assert resp.status_code == 200
    # 裁决本身成功了（200），结果状态是 failed（可重试）
    assert resp.json()["outcome"] == "resolved"
    assert resp.json()["publish"]["status"] == "failed"
    assert len(x.created) == 1  # 裁决不发帖，重发要另外显式调 /publish


async def test_resolve_retry_requires_explicit_confirmation(session_factory):
    """confirm_maybe_duplicate 必须显式传 true，防止"顺手"发出这个决定。"""
    x = _client(post_error=XApiTransportError("read timed out", may_have_reached_x=True))
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            await api.post(f"/api/runs/{run_id}/publish")

            missing = await api.post(
                f"/api/runs/{run_id}/publish/resolve", json={"action": "retry"}
            )
            false_value = await api.post(
                f"/api/runs/{run_id}/publish/resolve",
                json={"action": "retry", "confirm_maybe_duplicate": False},
            )

    assert missing.status_code == 422
    assert false_value.status_code == 422
    assert len(x.created) == 1


async def test_resolve_mark_published_requires_post_id(session_factory):
    x = _client(post_error=XApiTransportError("read timed out", may_have_reached_x=True))
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            await api.post(f"/api/runs/{run_id}/publish")

            resp = await api.post(
                f"/api/runs/{run_id}/publish/resolve", json={"action": "mark_published"}
            )

    assert resp.status_code == 422


async def test_resolve_on_confirmed_publish_returns_409(session_factory):
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            run_id = started["run_id"]
            await api.post(f"/api/runs/{run_id}/publish")
            resp = await api.post(
                f"/api/runs/{run_id}/publish/resolve",
                json={"action": "retry", "confirm_maybe_duplicate": True},
            )

    assert resp.status_code == 409


async def test_resolve_unknown_run_returns_404(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            resp = await api.post(
                f"/api/runs/{uuid.uuid4()}/publish/resolve",
                json={"action": "retry", "confirm_maybe_duplicate": True},
            )

    assert resp.status_code == 404


# ---------- 与 Phase 3 的隔离 ----------


async def test_review_endpoint_is_unaffected_by_publish(session_factory):
    """发布不改变 review 的行为：审核仍然是独立的一步。"""
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            run_id = started["run_id"]
            approved = await _approve(api, run_id)
            published = await api.post(f"/api/runs/{run_id}/publish")
            # 已经离开 waiting_for_review，再提交审核必须 409
            again = await api.post(f"/api/runs/{run_id}/review", json={"action": "approve"})

    assert approved["status"] == "approved"
    assert published.status_code == 200
    assert again.status_code == 409
    assert len(x.created) == 1


async def test_published_content_is_used_by_guardrail_dedup(session_factory):
    """发布成功后，正文进入 Guardrail 的判重来源。

    fake LLM 每次都产出同一份草稿，所以第二个 Run 必然与已发布内容重复。
    """
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            await api.post(f"/api/runs/{started['run_id']}/publish")

            second = await _start_run(api)

    assert second["guardrail_passed"] is False
    assert "duplicate_content" in second["guardrail_reasons"]
