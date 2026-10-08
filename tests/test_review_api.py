import asyncio
import uuid

from sqlalchemy import select, text

from app.main import Runtime
from app.models import AgentRun, Draft, ResearchItem, Review
from app.models.review import DECIDED_BY_AUTO, DECIDED_BY_HUMAN
from app.repositories import (
    claim_run_for_review,
    create_agent_run,
    create_review,
    mark_run_waiting_for_review,
)
from app.services.review import resume_review
from fakes import FakeLLMService
from support import FlakyGraph, api_for, runtime_with_checkpointer


async def _start_run(api, topic: str = "AI coding agent") -> dict:
    resp = await api.post("/api/runs", json={"topic": topic})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "waiting_for_review"
    return resp.json()


async def _run_row(session_factory, run_id: str) -> AgentRun:
    async with session_factory() as session:
        return await session.get(AgentRun, uuid.UUID(run_id))


async def _reviews(session_factory) -> list[Review]:
    async with session_factory() as session:
        return list((await session.execute(select(Review))).scalars().all())


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return len((await session.execute(select(model))).scalars().all())


async def test_approve_ends_run_as_approved(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            resp = await api.post(
                f"/api/runs/{started['run_id']}/review", json={"action": "approve"}
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["human_action"] == "approve"
    assert data["final_content"] == started["draft_content"]

    run = await _run_row(session_factory, started["run_id"])
    assert run.status == "approved"
    # approved 不是终态：这个 Run 还要走发布，finished_at 必须保持为空。
    # 若这里非空，"流水线跑完了"和"这个 Run 结束了"就再也分不开。
    assert run.finished_at is None

    review = (await _reviews(session_factory))[0]
    assert review.status == "approve"
    assert review.original_content == started["draft_content"]
    assert review.edited_content is None
    assert review.reject_reason is None
    assert review.reviewed_at is not None

    # resume 只重放被 interrupt 的 human_review，research / generate 不重跑：
    # 否则会重复抓取、重复生成草稿
    assert await _count(session_factory, ResearchItem) == 2
    assert await _count(session_factory, Draft) == 1


async def test_edit_keeps_original_draft_and_stores_edited_content(session_factory):
    edited = "人工改写：agents 正在改变软件的交付方式。"
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            resp = await api.post(
                f"/api/runs/{started['run_id']}/review",
                json={"action": "edit", "content": edited},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "edited"
    assert data["human_action"] == "edit"
    assert data["final_content"] == edited
    assert data["draft_content"] == started["draft_content"]  # 草稿未被改写

    run = await _run_row(session_factory, started["run_id"])
    assert run.status == "edited"

    review = (await _reviews(session_factory))[0]
    assert review.status == "edit"
    assert review.original_content == started["draft_content"]
    assert review.edited_content == edited
    assert review.reject_reason is None

    # 原 Draft 行原样保留，且没有产生新版本
    async with session_factory() as session:
        drafts = (await session.execute(select(Draft))).scalars().all()
        assert len(drafts) == 1
        assert drafts[0].content == started["draft_content"]


async def test_reject_ends_run_without_final_content(session_factory):
    reason = "不符合账号定位"
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            resp = await api.post(
                f"/api/runs/{started['run_id']}/review",
                json={"action": "reject", "reason": reason},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert data["human_action"] == "reject"
    assert data["final_content"] is None

    run = await _run_row(session_factory, started["run_id"])
    assert run.status == "rejected"
    # rejected 是终态（不会再走发布）→ finished_at 必须有值。
    # 与上面 approved 的断言成对：两者一起钉住 finished_at 的语义。
    assert run.finished_at is not None

    review = (await _reviews(session_factory))[0]
    assert review.status == "reject"
    assert review.reject_reason == reason
    assert review.edited_content is None
    assert review.original_content == started["draft_content"]


async def test_invalid_action_is_rejected(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            resp = await api.post(
                f"/api/runs/{started['run_id']}/review", json={"action": "publish"}
            )

    assert resp.status_code == 422

    # Run 仍是等待人工，没有被这个非法请求改动
    run = await _run_row(session_factory, started["run_id"])
    assert run.status == "waiting_for_review"
    assert await _reviews(session_factory) == []


async def test_edit_without_content_is_rejected(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            for body in ({"action": "edit"}, {"action": "edit", "content": ""},
                         {"action": "edit", "content": "   "}):
                resp = await api.post(f"/api/runs/{started['run_id']}/review", json=body)
                assert resp.status_code == 422, body

    run = await _run_row(session_factory, started["run_id"])
    assert run.status == "waiting_for_review"


async def test_reject_without_reason_is_rejected(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            for body in ({"action": "reject"}, {"action": "reject", "reason": "  "}):
                resp = await api.post(f"/api/runs/{started['run_id']}/review", json=body)
                assert resp.status_code == 422, body

    run = await _run_row(session_factory, started["run_id"])
    assert run.status == "waiting_for_review"


async def test_review_unknown_run_returns_404(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            resp = await api.post(
                "/api/runs/00000000-0000-0000-0000-000000000000/review",
                json={"action": "approve"},
            )

    assert resp.status_code == 404


async def test_review_twice_returns_409(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            first = await api.post(
                f"/api/runs/{started['run_id']}/review", json={"action": "approve"}
            )
            second = await api.post(
                f"/api/runs/{started['run_id']}/review", json={"action": "approve"}
            )
            # 场景 B：客户端没收到第一次的响应而重发。权威结果始终可以通过 GET 查到
            detail = (await api.get(f"/api/runs/{started['run_id']}")).json()

    assert first.status_code == 200
    assert second.status_code == 409  # 不会重复 resume、不会重复写 Review
    # 只产生一条 Review
    assert len(await _reviews(session_factory)) == 1

    run = await _run_row(session_factory, started["run_id"])
    assert run.status == "approved"
    assert detail["status"] == "approved"
    assert detail["human_action"] == "approve"
    assert detail["final_content"] == started["draft_content"]


async def test_review_on_failed_run_returns_409(session_factory):
    llm = FakeLLMService(text="not-json")  # 让 Generate 失败 → run failed
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        async with api_for(runtime) as api:
            failed = await api.post("/api/runs", json={"topic": "AI coding agent"})
            assert failed.status_code == 502
            run_id = failed.json()["run_id"]
            resp = await api.post(f"/api/runs/{run_id}/review", json={"action": "approve"})

    assert resp.status_code == 409


async def test_concurrent_reviews_only_one_wins(session_factory):
    """两个审核请求同时提交：只能有一个 resume，另一个必须被拒。"""
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            url = f"/api/runs/{started['run_id']}/review"
            responses = await asyncio.gather(
                api.post(url, json={"action": "approve"}),
                api.post(url, json={"action": "edit", "content": "另一个决定"}),
            )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409], [r.text for r in responses]
    # 只有胜出的那个决定落库，且只落一条
    assert len(await _reviews(session_factory)) == 1


async def test_resume_failure_keeps_run_recoverable(session_factory):
    """场景 A：CAS 抢到了，但 Graph resume 失败。

    此时 Graph 仍停在 human_review（checkpoint 完好），Run 事实上还在等人工，
    因此状态必须回到 waiting_for_review，让同一个人工决定可以重试。若直接标成
    failed，一次瞬时故障就会让 Run 永久不可恢复。
    """
    async with runtime_with_checkpointer(session_factory) as runtime:
        flaky = FlakyGraph(runtime.graph)
        async with api_for(
            Runtime(
                graph=flaky,
                session_factory=runtime.session_factory,
                publish_service=runtime.publish_service,
            )
        ) as api:
            started = await _start_run(api)
            run_id = started["run_id"]

            flaky.arm()
            failed = await api.post(f"/api/runs/{run_id}/review", json={"action": "approve"})

            # HTTP 层：请求失败 → 非 2xx；业务层：Run 没被这个故障毁掉
            assert failed.status_code == 500
            assert failed.json()["status"] == "waiting_for_review"
            assert "simulated checkpointer outage" in failed.json()["error"]

            run = await _run_row(session_factory, run_id)
            assert run.status == "waiting_for_review"
            assert run.finished_at is None  # 流程未结束
            assert run.error is not None  # 错误仍被保留，便于排查
            assert await _reviews(session_factory) == []  # 没有任何决定落库

            # checkpoint 没有被破坏，Graph 仍可恢复
            snapshot = await runtime.graph.aget_state({"configurable": {"thread_id": run_id}})
            assert snapshot.next == ("human_review",)

            # —— 重试同一个人工决定：应当成功恢复 ——
            retry = await api.post(f"/api/runs/{run_id}/review", json={"action": "approve"})

    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "approved"
    # 草稿来自 checkpoint（重试请求没有携带任何草稿内容）
    assert retry.json()["draft_content"] == started["draft_content"]

    run = await _run_row(session_factory, run_id)
    assert run.status == "approved"
    assert run.error is None  # 上一次的失败痕迹不残留在成功的 Run 上
    assert len(await _reviews(session_factory)) == 1


async def test_retry_after_partial_write_does_not_duplicate_review(session_factory):
    """场景 A 的残留窗口：上次 resume 已写下 review 行、但 checkpoint 未写成。

    重试会重放 human_review 节点，此时必须复用同一行而不是插入第二条，
    并且最终落库的决定要与 Graph 的最终判定一致。
    """
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            run_id = started["run_id"]

            # 模拟「节点已写库，但 checkpoint 写失败」：直接写下上次的决定
            async with session_factory() as session:
                await create_review(
                    session,
                    draft_id=started["draft_id"],
                    action="approve",
                    original_content=started["draft_content"],
                )

            edited = "人工改写：agents 正在改变软件的交付方式。"
            resp = await api.post(
                f"/api/runs/{run_id}/review", json={"action": "edit", "content": edited}
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "edited"
    reviews = await _reviews(session_factory)
    assert len(reviews) == 1  # 没有第二条
    assert reviews[0].status == "edit"  # 与 Graph 最终判定一致
    assert reviews[0].edited_content == edited


async def test_claim_run_for_review_is_atomic_under_true_concurrency(session_factory):
    """场景 C：两个独立 session 同时执行 CAS。

    依赖 PostgreSQL 的行锁 + READ COMMITTED 重检：后到的那条 UPDATE 会先阻塞，
    待前者提交后重新判断 WHERE，拿到 0 行。不需要任何锁服务。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, run_id)

    async def claim(new_status: str) -> bool:
        async with session_factory() as session:
            return await claim_run_for_review(
                session, run_id, expected="waiting_for_review", new_status=new_status
            )

    approved, edited = await asyncio.gather(claim("approved"), claim("edited"))

    assert sorted([approved, edited]) == [False, True]
    # 获胜者的状态被保留，失败者没有覆盖它
    assert (await _run_row(session_factory, run_id)).status in {"approved", "edited"}


async def test_get_run_detail(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            run_id = started["run_id"]

            waiting = await api.get(f"/api/runs/{run_id}")
            assert waiting.status_code == 200
            detail = waiting.json()
            assert detail["run_id"] == run_id
            assert detail["topic"] == "AI coding agent"
            assert detail["status"] == "waiting_for_review"
            assert detail["draft_content"] == started["draft_content"]
            assert detail["guardrail_passed"] is True
            assert detail["guardrail_reasons"] == []
            assert detail["human_action"] is None
            assert detail["final_content"] is None
            assert detail["review"] is None  # 还没人工决定

            await api.post(f"/api/runs/{run_id}/review", json={"action": "reject", "reason": "no"})

            decided = (await api.get(f"/api/runs/{run_id}")).json()

    assert decided["status"] == "rejected"
    assert decided["human_action"] == "reject"
    assert decided["final_content"] is None
    assert decided["review"]["action"] == "reject"
    assert decided["review"]["reject_reason"] == "no"
    assert decided["review"]["original_content"] == started["draft_content"]
    assert decided["review"]["reviewed_at"] is not None


async def test_claim_run_for_review_is_atomic(session_factory):
    """并发保护的原子性：同一个 Run 只能被抢到一次。"""
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, run_id)

    async with session_factory() as session:
        first = await claim_run_for_review(
            session, run_id, expected="waiting_for_review", new_status="approved"
        )
    async with session_factory() as session:
        second = await claim_run_for_review(
            session, run_id, expected="waiting_for_review", new_status="edited"
        )

    assert first is True
    assert second is False
    # 第二个请求没有覆盖第一个决定的状态
    assert (await _run_row(session_factory, run_id)).status == "approved"


async def test_get_unknown_run_returns_404(session_factory):
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            resp = await api.get("/api/runs/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404


async def test_resume_from_postgres_checkpoint_after_runtime_rebuild(session_factory):
    """核心恢复测试：interrupt 之后彻底重建 runtime，再用同一 thread_id resume。

    第二个 runtime 是全新的 checkpointer 连接池 + 全新编译的 Graph，进程内没有
    任何东西被复用；它能 resume 出原始草稿，只可能来自 PostgreSQL checkpoint。
    """
    # —— 第一个 runtime：跑到 interrupt ——
    async with runtime_with_checkpointer(session_factory) as runtime_a:
        async with api_for(runtime_a) as api_a:
            started = await _start_run(api_a)
            run_id = started["run_id"]
        snapshot_a = await runtime_a.graph.aget_state(
            {"configurable": {"thread_id": run_id}}
        )
        assert snapshot_a.next == ("human_review",)
        assert snapshot_a.values["draft_content"] == started["draft_content"]
    # runtime_a 的连接池此刻已关闭，graph 对象不再持有任何可用连接

    # checkpoint 确实落在 PostgreSQL 里
    async with session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"),
                {"tid": run_id},
            )
        ).scalar()
    assert rows >= 1

    # —— 第二个 runtime：全新实例，只有 run_id 是共享的 ——
    async with runtime_with_checkpointer(session_factory) as runtime_b:
        assert runtime_b.graph is not runtime_a.graph
        async with api_for(runtime_b) as api_b:
            resp = await api_b.post(f"/api/runs/{run_id}/review", json={"action": "approve"})

        assert resp.status_code == 200
        resumed = resp.json()
        assert resumed["status"] == "approved"
        # 这些值只能来自 checkpoint（runtime_b 从未收到过原始输入）
        assert resumed["draft_content"] == started["draft_content"]
        assert resumed["draft_id"] == started["draft_id"]
        assert resumed["final_content"] == started["draft_content"]
        assert resumed["guardrail_passed"] is True

        snapshot_b = await runtime_b.graph.aget_state({"configurable": {"thread_id": run_id}})
        assert snapshot_b.next == ()
        assert snapshot_b.values["human_action"] == "approve"

    run = await _run_row(session_factory, run_id)
    assert run.status == "approved"
    assert len(await _reviews(session_factory)) == 1


# ---------------------------------------------------------------------------
# `decided_by`：HTTP 结构性地无法声称自己是自动的
#
# 这一节守的是"机器人只是替你点了一下批准"这句话在数据上的样子。它不是一条
# 鉴权，而是一条**结构性**的保证：`decided_by` 刻意不在决策 schema 里，
# 而那个 schema 配着 `extra="ignore"`。
# ---------------------------------------------------------------------------


async def test_http_cannot_forge_decided_by_auto(session_factory):
    """客户端在请求体里塞 `decided_by: "auto"` —— 会被丢掉，不是被信任。

    为什么要紧：`decided_by` 是 `reap_stale_review_claims` 之类"清理自动残留"
    的判据，也是影子一致率的归因依据。若它能被 HTTP 写进来，任何客户端都能
    把一条人工 Run 伪装成自动的，而后果是那条 Run 会被自动清理逻辑收走。

    这里断言的是**结果**而不是"有没有某段校验代码"：即便将来有人给决策
    schema 加了这个字段，只要它仍然落到 `human`，这条测试就还是绿的——
    而它该绿就绿，因为要把 HTTP 路径真的打开需要同时改 schema 和这里。
    """
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            resp = await api.post(
                f"/api/runs/{started['run_id']}/review",
                json={"action": "approve", "decided_by": DECIDED_BY_AUTO},
            )
            assert resp.status_code == 200, resp.text

    reviews = await _reviews(session_factory)
    assert len(reviews) == 1
    assert reviews[0].decided_by == DECIDED_BY_HUMAN
    # 决定本身照常生效——被丢掉的只有"谁做的"这个声明。
    assert reviews[0].status == "approve"


async def test_the_auto_path_records_decided_by_auto_and_is_not_reaped(session_factory):
    """自动侧走**原始 resume dict** 这条路，于是标记得以保留。

    这条路只有本进程内部的代码走得通（HTTP 那条被 schema 挡住），
    这正是它的安全属性。

    后半段断言的是这条标记的**用处**：一条被自动放行的 Run 有 review 行，
    所以 reaper 的"可发布状态 + 没有 review 行"判据为假——自动放行的 Run
    不会被自家的清理逻辑当成残留收走。
    """
    from app.repositories import reap_stale_review_claims

    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
        run_id = started["run_id"]

        await resume_review(
            runtime,
            run_id,
            action="approve",
            # 自动侧**绕开** schema 直接给 Command(resume=...) 传原始 dict
            resume_value={"action": "approve", "decided_by": DECIDED_BY_AUTO},
        )

    reviews = await _reviews(session_factory)
    assert len(reviews) == 1
    assert reviews[0].decided_by == DECIDED_BY_AUTO

    run = await _run_row(session_factory, run_id)
    assert run.status == "approved"

    async with session_factory() as session:
        reaped = await reap_stale_review_claims(session)
    assert reaped == []
