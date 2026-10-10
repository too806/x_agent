"""运营控制台只读 Admin API 的测试（Phase 4b）。

测试分两类，各自解决不同的问题：

1. **播种式**（直接用 repositories 造数据）：用来精确构造某一种状态组合，
   验证过滤、分页、聚合的**正确性**。这类不需要完整 Graph。
2. **全链路**：真的跑一遍 `POST /api/runs`，再用 Admin API 读回来，验证控制台
   看到的东西与 Graph 真正产出的东西一致（尤其是只存在于 checkpoint 的
   guardrail 结论）。

刻意覆盖的几个"反例"：读不到 checkpoint 时必须退化成 `—` 而不是报错、不存在的
发布记录不得被 Run 状态顶替、LIKE 元字符必须转义、无数据时不得用 0 冒充不可计算
的指标。
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.main import Runtime
from app.models import AgentRun
from app.models.agent_run import ORIGIN_SCHEDULED
from app.models.auto_review import MODE_SHADOW, VERDICT_APPROVE, VERDICT_REJECT
from app.models.review import DECIDED_BY_AUTO
from app.repositories import (
    claim_publish,
    claim_run_for_review,
    create_agent_run,
    create_decision,
    create_draft,
    create_review,
    insert_auto_review,
    mark_publish_failed,
    mark_published,
    mark_run_waiting_for_review,
    note_publish_unknown,
    record_decision_result,
)
from fakes import StubGraph
from support import PERSONA, api_for, runtime_with_checkpointer

# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------


@asynccontextmanager
async def admin_api(session_factory, *, persona=PERSONA, state: dict | None = None):
    """把一个只用于控制台测试的 Runtime 注入 FastAPI。

    graph 用 `StubGraph()`（空 state）而不是真实 checkpointer：这类测试是**播种式**
    的，直接用 repositories 造出想要的状态组合，不需要跑 Graph。

    "读不到 checkpoint" 本身也是必须覆盖的真实情形（Run 刚 interrupt 的瞬间就是
    这样），所以空 state 不是妥协，而是刻意选定的输入。

    `state` 只有需要**某几个 checkpoint 字段**的测试才传（Phase 5B.4 的
    Run Detail 反馈块就是这种情况：它必须区分"键在、内容为空"与"键根本不在"）。
    默认的空字典仍然是绝大多数测试的输入。

    publish_service 传 None：Admin API 是只读的，任何路径都不该碰到它。真碰到了
    会直接 AttributeError 炸掉测试 —— 这本身就是"控制台没有偷偷写库"的哨兵。

    persona 显式传入而不是走 Runtime 的默认值：默认值会去读 config/persona.yaml，
    那样测试就会跟着配置文件改动而漂移。
    """
    runtime = Runtime(
        graph=StubGraph(state),
        session_factory=session_factory,
        publish_service=None,
        persona=persona,
    )
    async with api_for(runtime) as api:
        yield api


async def _make_run(session_factory, topic: str = "AI coding agent") -> str:
    async with session_factory() as session:
        run = await create_agent_run(session, topic)
        return str(run.id)


async def _make_goal_run(session_factory, goal: str = "找一个值得讨论的 AI 话题") -> str:
    """造一个 Phase 5A 的 Agent 模式 Run：只有目标，主题还没有。

    topic 为 None 是它的**真实初始状态**（Agent 还没选），不是构造出来的残缺数据。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, topic=None, goal=goal)
        return str(run.id)


async def _make_waiting_run(session_factory, topic: str, *, at: datetime | None = None) -> str:
    """造一条**待人工审核**的 Run，可选地把 `created_at` 钉到指定时刻。

    `created_at` 由 server_default 生成、构造不出历史时间，队列排序的测试因此必须
    走 `_set_created_at` 这条绕过 ORM 的路（见那个函数的说明）。不传 `at` 就保持
    建库时的当前时间，用于不关心顺序的用例。
    """
    run_id = await _make_run(session_factory, topic)
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, run_id)
    if at is not None:
        await _set_created_at(session_factory, run_id, at)
    return run_id


async def _make_auto_waiting_run(
    session_factory,
    topic: str,
    *,
    run_origin: str = ORIGIN_SCHEDULED,
    mode: str = MODE_SHADOW,
    verdict: str = VERDICT_REJECT,
    reasons: list | None = None,
    critic_score: float | None = None,
    acted: bool = False,
    with_auto_review: bool = True,
) -> tuple[str, str | None]:
    """造一条**自动调度建出来、自动审阅过、仍等人审**的 Run。

    真实时序是：tick 建 run（origin=scheduled）→ 跑 Graph → Graph 在 human_review
    中断（此时草稿已写）→ 判据落 auto_review 行 → 到此为止（影子模式不动作）。
    这里逐步复刻，不跳步——"run 在 waiting_for_review 且有 auto_review 行"正是
    影子模式唯一稳定的落库形态。

    `with_auto_review=False` 造的是**正式模式**下判据放行了的那条：run 已经被自动
    放行、正在走发布，队列里看不到它。控制台不该因为看不到就以为它没有自动结论。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, topic, origin=run_origin)
        run_id = str(run.id)
    async with session_factory() as session:
        draft_id = await create_draft(
            session, run_id, content="草稿正文", version=1, prompt_version="v1", model_name="fake"
        )
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, run_id)
    if with_auto_review:
        async with session_factory() as session:
            inserted = await insert_auto_review(
                session,
                agent_run_id=run_id,
                draft_id=draft_id,
                mode=mode,
                verdict=verdict,
                reasons=reasons if reasons is not None else ["内容里有硬问题。"],
                checks={"duplication": {"hit": False}},
                critic_score=critic_score,
                acted=acted,
            )
            assert inserted is not None, "auto_review 行没插进去，测试前提不成立"
    return run_id, draft_id


async def _seed_decision(
    session_factory,
    run_id: str,
    *,
    step: int,
    action: str,
    reason: str,
    result: str = "",
    target: str | None = None,
) -> None:
    """落一步决策，并按真实时序回填结果（Planner 先写意图、执行完再补结果）。"""
    async with session_factory() as session:
        await create_decision(
            session,
            run_id=run_id,
            step=step,
            action=action,
            reason=reason,
            target=target,
            input_summary=f"topics={step}; step={step}/12",
        )
    if result:
        async with session_factory() as session:
            await record_decision_result(session, run_id=run_id, step=step, result_summary=result)


async def _make_reviewed_run(session_factory, topic: str, action: str = "approve") -> tuple[str, str]:
    """造一个已审核通过的 Run，返回 (run_id, draft_id)。"""
    run_id = await _make_run(session_factory, topic)
    async with session_factory() as session:
        await claim_run_for_review(
            session, run_id, expected="running", new_status={"approve": "approved", "edit": "edited"}[action]
        )
    async with session_factory() as session:
        draft_id = await create_draft(
            session, run_id, content="草稿正文", version=1, prompt_version="v1", model_name="fake"
        )
    async with session_factory() as session:
        await create_review(
            session, draft_id=draft_id, action=action, original_content="草稿正文"
        )
    return run_id, draft_id


async def _publish_as(session_factory, run_id: str, draft_id: str, outcome: str) -> None:
    """把一次发布推进到指定结局，全部走 repositories 的真实路径。"""
    async with session_factory() as session:
        claimed = await claim_publish(
            session, agent_run_id=run_id, draft_id=draft_id, body="草稿正文"
        )
        assert claimed is True
    if outcome == "published":
        async with session_factory() as session:
            assert await mark_published(session, agent_run_id=run_id, x_post_id="1234567890")
    elif outcome == "failed":
        async with session_factory() as session:
            assert await mark_publish_failed(session, agent_run_id=run_id, error="X 明确拒绝")
    elif outcome == "publish_unknown":
        async with session_factory() as session:
            await note_publish_unknown(session, agent_run_id=run_id, error="读超时")
    elif outcome != "publishing":  # publishing = 抢占后什么都不做
        raise AssertionError(f"unknown outcome {outcome}")


async def _set_publish_created_at(session_factory, run_id: str, when: datetime) -> None:
    """绕过 ORM 改 published_post.created_at —— 同样只由 server_default 生成。

    发布记录列表的日期筛选走的是这一列（`published_post.created_at`），不是
    `published_at`：后者只有 published 的行才有值，用它筛会让 failed / unknown
    既不出现在窗口里，也不出现在窗口外，像是从来不存在。
    """
    async with session_factory() as session:
        await session.execute(
            text("UPDATE published_post SET created_at = :ts WHERE agent_run_id = :id"),
            {"ts": when, "id": uuid.UUID(run_id)},
        )
        await session.commit()


async def _set_created_at(session_factory, run_id: str, when: datetime) -> None:
    """绕过 ORM 直接改 created_at —— 它由 server_default 生成，构造不出来历史时间。"""
    async with session_factory() as session:
        await session.execute(
            text("UPDATE agent_run SET created_at = :ts WHERE id = :id"),
            {"ts": when, "id": uuid.UUID(run_id)},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


async def test_overview_empty_state_is_all_zeros(session_factory):
    """空库必须给出结构完整的响应，而不是 500 或 null。"""
    async with admin_api(session_factory) as api:
        resp = await api.get("/api/admin/overview")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["kpis"] == {
        "total_runs": 0,
        "waiting_for_review": 0,
        "published": 0,
        "publish_failed": 0,
        "publish_unknown": 0,
        "publishing": 0,
    }
    assert data["review_queue"] == []
    assert data["resolution_queue"] == []
    assert data["review_queue_total"] == 0
    assert data["resolution_queue_total"] == 0
    # 趋势补齐到整窗口：没数据的日期是 0，不是缺项
    assert len(data["runs_trend"]) == 7
    assert {point["count"] for point in data["runs_trend"]} == {0}
    assert len(data["published_trend"]) == 7


async def test_overview_kpis_come_from_published_post_not_run_status(session_factory):
    """发布三项 KPI 一律取自 published_post —— 它是发布状态的权威。

    这里刻意让 Run 状态与发布状态**不一致**（发布失败时 Run 被还原成 approved），
    如果实现偷懒用了 agent_run.status，published/failed 的计数就会错。
    """
    ok_run, ok_draft = await _make_reviewed_run(session_factory, "已发布的题")
    await _publish_as(session_factory, ok_run, ok_draft, "published")

    fail_run, fail_draft = await _make_reviewed_run(session_factory, "发布失败的题")
    await _publish_as(session_factory, fail_run, fail_draft, "failed")

    unknown_run, unknown_draft = await _make_reviewed_run(session_factory, "结果未知的题")
    await _publish_as(session_factory, unknown_run, unknown_draft, "publish_unknown")

    stuck_run, stuck_draft = await _make_reviewed_run(session_factory, "卡住的题")
    await _publish_as(session_factory, stuck_run, stuck_draft, "publishing")

    await _make_run(session_factory, "还在跑的题")

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/overview")).json()

    assert data["kpis"]["published"] == 1
    assert data["kpis"]["publish_failed"] == 1
    assert data["kpis"]["publish_unknown"] == 1
    assert data["kpis"]["publishing"] == 1
    assert data["kpis"]["total_runs"] == 5

    # 发布失败**不是**流水线失败：Run 被还原成 approved，绝不是 failed。
    async with session_factory() as session:
        row = await session.get(AgentRun, uuid.UUID(fail_run))
        assert row.status == "approved"


async def test_overview_queues_surface_pending_work(session_factory):
    waiting = await _make_run(session_factory, "等待审核的题")
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, waiting)

    unknown_run, unknown_draft = await _make_reviewed_run(session_factory, "未知的题")
    await _publish_as(session_factory, unknown_run, unknown_draft, "publish_unknown")

    stuck_run, stuck_draft = await _make_reviewed_run(session_factory, "卡住的题")
    await _publish_as(session_factory, stuck_run, stuck_draft, "publishing")

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/overview")).json()

    assert data["kpis"]["waiting_for_review"] == 1
    assert [item["run_id"] for item in data["review_queue"]] == [waiting]
    assert data["review_queue_total"] == 1

    # 裁决队列：publish_unknown 排在 publishing 前面（前者已结束，能立刻处置）
    assert data["resolution_queue_total"] == 2
    statuses = [item["publish"]["status"] for item in data["resolution_queue"]]
    assert statuses == ["publish_unknown", "publishing"]
    assert {item["run_id"] for item in data["resolution_queue"]} == {unknown_run, stuck_run}


async def test_overview_does_not_invent_publish_status(session_factory):
    """没有 published_post 行的 Run 不得出现在裁决队列里。

    Run 停在 `publishing` 却没有发布行是不该发生的状态，但万一发生，控制台必须
    显示"没有记录"，而不是替它猜一个发布状态。
    """
    run_id = (await _make_reviewed_run(session_factory, "没有发布记录的题"))[0]

    async with admin_api(session_factory) as api:
        runs = (await api.get("/api/admin/runs")).json()
        detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    assert runs["items"][0]["run_id"] == run_id
    # None 表示"没有发布记录"，不是某个兜底状态
    assert runs["items"][0]["publish_status"] is None
    assert runs["items"][0]["review_status"] == "approve"
    assert detail["publish"] is None


# ---------------------------------------------------------------------------
# Runs：分页 / 过滤 / 搜索
# ---------------------------------------------------------------------------


async def test_runs_pagination_is_server_side(session_factory):
    """分页必须由服务器完成：页大小真正生效，且跨页不重不漏。"""
    created = []
    for index in range(5):
        run_id = await _make_run(session_factory, f"题 {index}")
        await _set_created_at(
            session_factory, run_id, datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=index)
        )
        created.append(run_id)

    async with admin_api(session_factory) as api:
        first = (await api.get("/api/admin/runs?page=1&page_size=2")).json()
        second = (await api.get("/api/admin/runs?page=2&page_size=2")).json()
        third = (await api.get("/api/admin/runs?page=3&page_size=2")).json()

    assert first["page"] == {"page": 1, "page_size": 2, "total": 5, "pages": 3}
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert len(third["items"]) == 1  # 最后一页不满

    # 倒序（最新在前），且三页合起来正好是全部 5 条
    seen = [item["run_id"] for item in first["items"] + second["items"] + third["items"]]
    assert seen == list(reversed(created))
    assert len(set(seen)) == 5


async def test_runs_page_size_is_capped(session_factory):
    """page_size 有上限：不允许用超大页把整张表拖进内存。"""
    async with admin_api(session_factory) as api:
        too_big = await api.get("/api/admin/runs?page_size=100000")
        invalid_page = await api.get("/api/admin/runs?page=0")

    assert too_big.status_code == 422
    assert invalid_page.status_code == 422


async def test_runs_status_filter_and_topic_search(session_factory):
    waiting = await _make_run(session_factory, "agents in production")
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, waiting)
    approved, _ = await _make_reviewed_run(session_factory, "agents at scale")

    async with admin_api(session_factory) as api:
        by_status = (await api.get("/api/admin/runs?status=waiting_for_review")).json()
        by_topic = (await api.get("/api/admin/runs?topic=at scale")).json()
        no_match = (await api.get("/api/admin/runs?topic=zzz")).json()
        unknown_status = (await api.get("/api/admin/runs?status=not_a_status")).json()

    assert [i["run_id"] for i in by_status["items"]] == [waiting]
    assert [i["run_id"] for i in by_topic["items"]] == [approved]
    assert no_match["items"] == [] and no_match["page"]["total"] == 0
    # 未知状态是空结果，不是错误——过滤是字面量等值匹配
    assert unknown_status["items"] == []


async def test_topic_search_escapes_like_wildcards(session_factory):
    """`_` 和 `%` 必须按字面量匹配。

    不转义的话，搜索框里敲一个 `_` 会命中所有 Run、敲 `%` 也一样——用户会以为
    搜索坏了。
    """
    underscore = await _make_run(session_factory, "AI_agent")
    await _make_run(session_factory, "AIXagent")

    async with admin_api(session_factory) as api:
        result = (await api.get("/api/admin/runs?topic=AI_")).json()
        percent = (await api.get("/api/admin/runs?topic=%25")).json()

    assert [i["run_id"] for i in result["items"]] == [underscore]
    assert percent["items"] == []


async def test_runs_date_range_is_half_open(session_factory):
    """日期过滤是 [from, to)：左闭右开，边界那一天只算一次。"""
    inside = await _make_run(session_factory, "窗口内")
    await _set_created_at(session_factory, inside, datetime(2026, 9, 10, 12, tzinfo=timezone.utc))
    boundary = await _make_run(session_factory, "正好在右端")
    await _set_created_at(session_factory, boundary, datetime(2026, 9, 11, tzinfo=timezone.utc))

    async with admin_api(session_factory) as api:
        window = (
            await api.get(
                "/api/admin/runs?date_from=2026-09-10T00:00:00Z&date_to=2026-09-11T00:00:00Z"
            )
        ).json()

    assert [i["run_id"] for i in window["items"]] == [inside]
    assert window["page"]["total"] == 1


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


async def test_run_detail_unknown_run_returns_404(session_factory):
    async with admin_api(session_factory) as api:
        resp = await api.get("/api/admin/runs/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404


async def test_run_detail_reads_real_research_draft_and_checkpoint(session_factory):
    """全链路：真跑一遍 Graph，再用控制台读回来。

    验证控制台展示的 research / draft 来自**数据库真实落库的行**，guardrail 来自
    checkpoint —— 也就是这个产品的完整生命周期在控制台里确实能看到。
    """
    async with runtime_with_checkpointer(session_factory) as runtime:
        # Admin 路由解析的 get_runtime 是同一个对象，所以一个 client 就够。
        async with api_for(runtime) as api:
            started = (await api.post("/api/runs", json={"topic": "AI coding agent"})).json()
            run_id = started["run_id"]
            detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    assert detail["topic"] == "AI coding agent"
    assert detail["run_status"] == "waiting_for_review"
    assert detail["review_status"] is None
    assert detail["publish_status"] is None

    # research items 来自 research_item 表，不是 graph state
    assert len(detail["research_items"]) == len(started["research_results"]) == 2

    # 按 platform_post_id 定位，**不按下标**。
    #
    # 这里踩过一个坑：repository 的排序是 `ORDER BY created_at, id`，而
    # created_at 用的是 Postgres 的 now()（事务开始时刻）——同一个 Run 的研究条目
    # 在同一个事务里写入，时间戳完全相同；tie-break 落到 id 上，而 id 是 uuid4，
    # 是随机的。于是"第一条"在一半的运行里是 1002。
    #
    # 系统从来没有承诺过研究条目的顺序（对使用者而言，一屏材料按什么次序排并不
    # 重要），所以断言顺序本身就是错的。要看顺序问题，看 4b 的收口记录。
    by_post_id = {item["platform_post_id"]: item for item in detail["research_items"]}
    assert set(by_post_id) == {"1001", "1002"}
    assert by_post_id["1001"]["likes"] == 42
    assert by_post_id["1001"]["reposts"] == 5
    assert by_post_id["1001"]["replies"] == 3
    assert by_post_id["1001"]["author_username"] == "ada_dev"

    # draft 来自 draft 表
    assert detail["draft"]["content"] == started["draft_content"]
    assert detail["draft"]["draft_id"] == started["draft_id"]

    # guardrail 只存在于 checkpoint
    assert detail["guardrail_passed"] is True
    assert detail["guardrail_reasons"] == []
    assert detail["review"] is None
    assert detail["publish"] is None


async def test_run_detail_shows_both_original_and_edited_content(session_factory):
    """编辑路径：原稿与人工改写稿必须同时可见，且发布正文等于改写稿。"""
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = (await api.post("/api/runs", json={"topic": "AI coding agent"})).json()
            run_id = started["run_id"]
            edited = "人工改写后的正文"
            await api.post(f"/api/runs/{run_id}/review", json={"action": "edit", "content": edited})
            detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    assert detail["run_status"] == "edited"
    assert detail["review_status"] == "edit"
    assert detail["human_action"] == "edit"
    assert detail["final_content"] == edited
    assert detail["review"]["original_content"] == started["draft_content"]
    assert detail["review"]["edited_content"] == edited
    # 原 draft 行没有被覆盖
    assert detail["draft"]["content"] == started["draft_content"]


async def test_run_detail_survives_missing_checkpoint(session_factory):
    """读不到 checkpoint 时退化成 `—`，不是 500。

    直接播种一个没有 checkpoint 的 Run —— 这正是"库里有一行、但 Graph 没跑过"
    的情形。控制台必须仍然能展示数据库里那部分事实。
    """
    run_id = await _make_run(session_factory, "没有 checkpoint 的题")

    async with admin_api(session_factory) as api:
        resp = await api.get(f"/api/admin/runs/{run_id}")

    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["guardrail_passed"] is None
    assert detail["guardrail_reasons"] == []
    assert detail["run_status"] == "running"
    assert detail["research_items"] == []


# ---------------------------------------------------------------------------
# Agent 决策轨迹（Phase 5A）
# ---------------------------------------------------------------------------


async def test_run_detail_orders_the_decision_trace_by_step(session_factory):
    """决策轨迹按 **step** 排，不是按写入时刻。

    所以这里刻意倒着播种（先写第 2 步、再写第 1 步）：如果实现改成按 created_at
    排序，这个顺序就会翻过来。之所以必须按 step，是因为重放或补写会让写入时刻与
    逻辑顺序不一致，而 step 才是决策本身的序号。
    """
    run_id = await _make_goal_run(session_factory, "找一个值得讨论的 AI 话题")
    await _seed_decision(
        session_factory, run_id, step=2, action="research_topic", reason="材料还不够",
        result="主题「某模型开源」，取回 10 条材料", target="某模型开源",
    )
    await _seed_decision(
        session_factory, run_id, step=1, action="discover_topics", reason="开局先找选题",
        result="得到 5 个候选选题",
    )

    async with admin_api(session_factory) as api:
        detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    trace = detail["decision_trace"]
    assert [row["step"] for row in trace] == [1, 2]
    assert [row["action"] for row in trace] == ["discover_topics", "research_topic"]
    assert trace[1]["target"] == "某模型开源"
    assert trace[1]["result_summary"] == "主题「某模型开源」，取回 10 条材料"
    # 存的是"当时手上有什么"，不是完整 prompt
    assert trace[0]["input_summary"].startswith("topics=")
    assert trace[0]["created_at"]


async def test_run_detail_shows_goal_separately_from_topic(session_factory):
    """goal 与 topic 必须分列，且 topic 还没选出来时就是 null。

    不能拿 goal 去顶替 topic 显示：goal 是**人的输入**，topic 是 **Agent 的选择**，
    顶替之后界面会看起来像"Agent 已经选定了一个叫『找一个值得讨论的 AI 话题』的题"。
    """
    run_id = await _make_goal_run(session_factory, "围绕开源模型找一个角度")

    async with admin_api(session_factory) as api:
        detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    assert detail["goal"] == "围绕开源模型找一个角度"
    assert detail["topic"] is None
    assert detail["decision_trace"] == []


async def test_run_list_carries_goal_without_faking_a_topic(session_factory):
    """列表行同样如此 —— Agent 模式与 topic 模式在列表里要能区分。"""
    goal_run = await _make_goal_run(session_factory)
    topic_run = await _make_run(session_factory, "AI coding agent")

    async with admin_api(session_factory) as api:
        items = (await api.get("/api/admin/runs", params={"page_size": 100})).json()["items"]

    by_id = {item["run_id"]: item for item in items}
    assert by_id[goal_run]["goal"] == "找一个值得讨论的 AI 话题"
    assert by_id[goal_run]["topic"] is None
    # 既有链路的 Run 不该被塞上一个 goal
    assert by_id[topic_run]["goal"] is None
    assert by_id[topic_run]["topic"] == "AI coding agent"


# ---------------------------------------------------------------------------
# Review 队列
# ---------------------------------------------------------------------------


async def test_review_queue_is_oldest_first_and_paginated(session_factory):
    ids = []
    for index in range(3):
        run_id = await _make_run(session_factory, f"待审 {index}")
        async with session_factory() as session:
            await mark_run_waiting_for_review(session, run_id)
        await _set_created_at(
            session_factory, run_id, datetime(2026, 9, 5, tzinfo=timezone.utc) + timedelta(hours=index)
        )
        ids.append(run_id)

    # 一个已决定的 Run 不该出现在待审队列里
    decided = (await _make_reviewed_run(session_factory, "已审过的题"))[0]

    async with admin_api(session_factory) as api:
        page = (await api.get("/api/admin/reviews?page=1&page_size=2")).json()

    assert page["page"] == {"page": 1, "page_size": 2, "total": 3, "pages": 2}
    assert [i["run_id"] for i in page["items"]] == ids[:2]  # 最老的在前
    assert decided not in [i["run_id"] for i in page["items"]]

    async with admin_api(session_factory) as api:
        second = (await api.get("/api/admin/reviews?page=2&page_size=2")).json()
    assert [i["run_id"] for i in second["items"]] == ids[2:]


async def test_review_queue_newest_order_reverses_and_paginates(session_factory):
    """`order=newest` 是默认顺序的**反转**，而不是"另一种大概的排法"。

    它服务的是"刚跑完就想审这一条"：待审队列 FIFO、每页 20，队列几十条深时新的那条
    排在最后一页，翻过去要找半天。上面那个用例（`test_review_queue_is_oldest_first_...`）
    是默认没被改掉的回归锁，这一个守的是开关真的反过来了。
    """
    ids = []
    for index in range(3):
        run_id = await _make_waiting_run(
            session_factory,
            f"待审 {index}",
            at=datetime(2026, 9, 5, tzinfo=timezone.utc) + timedelta(hours=index),
        )
        ids.append(run_id)

    async with admin_api(session_factory) as api:
        newest = (await api.get("/api/admin/reviews?order=newest&page_size=2")).json()
        still_oldest = (await api.get("/api/admin/reviews?order=oldest&page_size=2")).json()

    assert [i["run_id"] for i in newest["items"]] == [ids[2], ids[1]]
    assert newest["page"] == {"page": 1, "page_size": 2, "total": 3, "pages": 2}
    # 同一个查询里显式传 oldest 仍是最老的在前——两个方向各自成立
    assert [i["run_id"] for i in still_oldest["items"]] == ids[:2]

    async with admin_api(session_factory) as api:
        second = (await api.get("/api/admin/reviews?order=newest&page=2&page_size=2")).json()
    assert [i["run_id"] for i in second["items"]] == [ids[0]]


async def test_review_queue_topic_search_counts_and_selects_the_same_rows(session_factory):
    """`topic` 必须**同时**作用于 COUNT 与 SELECT。

    `page.total` 由 COUNT 出、条目由 SELECT 出。只给 SELECT 加谓词的话，界面会显示
    "命中 3 条"却只列出 2 条、翻到第 2 页一片空白；只给 COUNT 加则相反。所以这里
    特意用 `page_size=1` 逼出翻页：总数与实际能翻出的条目必须一致，且都等于命中数。
    """
    earlier = await _make_waiting_run(
        session_factory, "agents in production", at=datetime(2026, 9, 5, tzinfo=timezone.utc)
    )
    later = await _make_waiting_run(
        session_factory,
        "agents at scale",
        at=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    unrelated = await _make_waiting_run(session_factory, "robot supply chain")

    async with admin_api(session_factory) as api:
        first = (await api.get("/api/admin/reviews?topic=agents&page_size=1")).json()
        second = (await api.get("/api/admin/reviews?topic=agents&page=2&page_size=1")).json()
        all_waiting = (await api.get("/api/admin/reviews?page_size=100")).json()

    # 命中 2 条，不是全部 3 条待审
    assert first["page"]["total"] == 2
    assert first["page"]["pages"] == 2
    assert [i["run_id"] for i in first["items"]] == [earlier]
    assert [i["run_id"] for i in second["items"]] == [later]
    # 没被搜到的只出现在不带筛选的那次里
    assert unrelated in [i["run_id"] for i in all_waiting["items"]]
    assert unrelated not in [i["run_id"] for i in first["items"]]


async def test_review_queue_topic_search_escapes_like_wildcards(session_factory):
    """待审队列的搜索与 `/runs` 的搜索同一套转义：`_` / `%` 按字面量匹配。

    这是 `_review_queue_clauses` 这个新的调用点：转义漏了的话，搜索框里敲一个 `_`
    会命中全部待审条目。
    """
    underscore = await _make_waiting_run(session_factory, "AI_agent")
    await _make_waiting_run(session_factory, "AIXagent")

    async with admin_api(session_factory) as api:
        result = (await api.get("/api/admin/reviews?topic=AI_")).json()
        percent = (await api.get("/api/admin/reviews?topic=%25")).json()

    assert [i["run_id"] for i in result["items"]] == [underscore]
    assert percent["items"] == [] and percent["page"]["total"] == 0


async def test_review_queue_combines_topic_with_order(session_factory):
    """两个参数是正交的：先筛后排，顺序作用在筛完的结果上。"""
    earlier = await _make_waiting_run(
        session_factory, "agents in production", at=datetime(2026, 9, 5, tzinfo=timezone.utc)
    )
    later = await _make_waiting_run(
        session_factory, "agents at scale", at=datetime(2026, 9, 6, tzinfo=timezone.utc)
    )
    await _make_waiting_run(session_factory, "robot supply chain")

    async with admin_api(session_factory) as api:
        page = (await api.get("/api/admin/reviews?topic=agents&order=newest")).json()

    assert [i["run_id"] for i in page["items"]] == [later, earlier]


async def test_review_queue_unknown_topic_is_empty_not_422(session_factory):
    """搜不到是**空页**，不是错误——与 `/runs` 的 topic 同一个立场（过滤是子串匹配）。"""
    await _make_waiting_run(session_factory, "agents in production")

    async with admin_api(session_factory) as api:
        no_match = await api.get("/api/admin/reviews?topic=zzz")

    assert no_match.status_code == 200
    assert no_match.json()["items"] == [] and no_match.json()["page"]["total"] == 0


async def test_review_queue_rejects_unknown_order(session_factory):
    """`order` 是唯一的白名单参数：它的取值是这个接口自己的两种形状，没有第二个源头
    需要同步，所以拼错时直接 422 比静默退回默认值更好——悄悄用一个和请求不符的顺序
    返回结果，会让人以为"最新优先"就是这个样子。"""
    await _make_waiting_run(session_factory, "agents in production")

    async with admin_api(session_factory) as api:
        bad = await api.get("/api/admin/reviews?order=asc")

    assert bad.status_code == 422


async def test_review_queue_reports_the_runtime_max_length(session_factory):
    """编辑字数上限必须来自当前 Runtime 的 persona，不能是写死的常量。

    用 137 这个不会出现在任何配置里的值：如果实现里把它写死了（140 或 280），
    这个测试会立刻失败。它守的是"界面上的 N / 140 和发布前的校验用的是同一个数"
    这条一致性——两者一旦分叉，计数就会开始骗人。
    """
    persona = PERSONA.model_copy(update={"max_length": 137})
    await _make_run(session_factory, "待审的题")

    async with admin_api(session_factory, persona=persona) as api:
        page = (await api.get("/api/admin/reviews")).json()

    assert page["content_max_length"] == 137


async def test_review_queue_preview_is_truncated(session_factory):
    """列表里只给预览，正文太长必须截断 —— 完整内容去详情看。"""
    async with runtime_with_checkpointer(session_factory) as runtime:
        async with api_for(runtime) as api:
            started = (await api.post("/api/runs", json={"topic": "AI coding agent"})).json()
            assert len(started["draft_content"]) < 200  # 前提：假草稿本来就短
            item = (await api.get("/api/admin/reviews")).json()["items"][0]

    assert item["draft_preview"] == started["draft_content"]
    assert item["guardrail_passed"] is True


async def test_review_queue_accepts_a_run_without_a_topic(session_factory):
    """Agent 模式的 Run 在 Planner 选定主题之前，topic 就是 NULL —— 队列必须容得下它。

    这不是构造出来的残缺数据：`finalize` 只在真的选到主题时才写 topic，所以
    "人在等审核、主题还没定"是一个真实的中间态。（topic 在 Agent 选定之前是 NULL，
    见 Phase 5A 设计文档。）

    曾经这里整个接口 500：`ReviewQueueItem.topic` 声明成 `str`，一条 NULL 就让
    `/api/admin/reviews` 和 `/api/admin/overview` 一起挂掉。这个测试守的是
    "一条没有主题的待审内容不能把整页打挂"，以及"读不到就如实返回 NULL"。
    """
    run_id = await _make_goal_run(session_factory)
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, run_id)

    async with admin_api(session_factory) as api:
        page = (await api.get("/api/admin/reviews")).json()
        overview = (await api.get("/api/admin/overview")).json()

    item = next(i for i in page["items"] if i["run_id"] == run_id)
    assert item["topic"] is None
    assert item["run_status"] == "waiting_for_review"

    queued = next(i for i in overview["review_queue"] if i["run_id"] == run_id)
    assert queued["topic"] is None
    assert overview["review_queue_total"] == 1


# ---------------------------------------------------------------------------
# 自动审阅在控制台上的可见性
# ---------------------------------------------------------------------------


async def test_review_queue_shows_an_auto_run_with_its_verdict(session_factory):
    """自动建出来的 Run 必须在队列里**自证身份**，并带上自动审阅说了什么。

    这条是整套自动发布里唯一由人看的一处：人打开队列，看到的是一个已经替他说了
    "不通过"的机器人。如果界面上只有一条普通的待审条目，人就会**重新从零审一遍**，
    影子模式量到的一致率就永远没人看、也永远翻不了正。

    `reasons` 必须原样透传，不能在服务层"整理"成一句概括——转正式前要回答
    "哪条判据在误拦"，靠的就是这几句。
    """
    run_id, _ = await _make_auto_waiting_run(
        session_factory,
        "自动跑出来的题",
        reasons=["Guardrail 命中了硬性问题（数据无出处）。", "今天已经发起过 1 次发布，达到上限 1。"],
        critic_score=8.4,
    )

    async with admin_api(session_factory) as api:
        page = (await api.get("/api/admin/reviews")).json()

    item = next(i for i in page["items"] if i["run_id"] == run_id)
    assert item["origin"] == ORIGIN_SCHEDULED
    assert item["auto_review"] == {
        "mode": MODE_SHADOW,
        "verdict": VERDICT_REJECT,
        "reasons": [
            "Guardrail 命中了硬性问题（数据无出处）。",
            "今天已经发起过 1 次发布，达到上限 1。",
        ],
        # 8.4 而不是 8：分数是 Critic 的原值，控制台不得四舍五入——
        # 门槛就是按它比的，抹掉尾数会让人对着"刚过线"的数字做错判断。
        "critic_score": 8.4,
        "acted": False,
    }


async def test_review_queue_does_not_attribute_one_runs_verdict_to_another(session_factory):
    """自动结论是按 run 装配的，**不许**串台。

    列表页为了不做 N+1，一个会话里把整页的 auto_review 批量取回来（`auto_reviews_by_run`），
    再按 run_id 分发。批量这一步的返回若被误当成"最后一条"或按位置对上，队列里
    就会出现**甲的机器意见挂在乙身上**——人会据此批掉一条机器从没看过的稿。
    所以这里造两条同时在队、结论不同的 Run，逐条断言。

    顺带钉住"没有自动结论 ≠ 没有结论"：手工 Run 的字段必须是 `None`，不能是空对象。
    """
    rejected, _ = await _make_auto_waiting_run(
        session_factory, "被判不通过的题", reasons=["重复了 14 天内的一条选题。"], critic_score=6.1
    )
    manual = await _make_waiting_run(session_factory, "人手发起的题")
    approved, _ = await _make_auto_waiting_run(
        session_factory,
        "被判通过的题",
        verdict=VERDICT_APPROVE,
        reasons=[],
        critic_score=9.2,
    )

    async with admin_api(session_factory) as api:
        page = (await api.get("/api/admin/reviews")).json()

    by_id = {i["run_id"]: i for i in page["items"]}
    assert by_id[rejected]["auto_review"]["verdict"] == VERDICT_REJECT
    assert by_id[rejected]["auto_review"]["critic_score"] == 6.1
    assert by_id[rejected]["auto_review"]["reasons"] == ["重复了 14 天内的一条选题。"]
    assert by_id[approved]["auto_review"]["verdict"] == VERDICT_APPROVE
    assert by_id[approved]["auto_review"]["critic_score"] == 9.2
    # 通过 = 无人可归因的理由，必须是空列表而不是 None（两种"没有"要分得开）
    assert by_id[approved]["auto_review"]["reasons"] == []
    assert by_id[manual]["origin"] == "manual"
    assert by_id[manual]["auto_review"] is None


async def test_run_detail_carries_the_same_auto_review_block(session_factory):
    """队列条目和 Run 详情必须说同一件事。

    人从队列点进去是为了看理由的全文，而队列只给了预览。两处若各装配一次、
    各写一遍字段，迟早会漂开——这里直接断言详情里的块与队列里的块**逐字段相等**。
    """
    run_id, _ = await _make_auto_waiting_run(
        session_factory, "详情页也要看到理由", reasons=["与已发内容高度雷同。"], critic_score=5.5
    )

    async with admin_api(session_factory) as api:
        page = (await api.get("/api/admin/reviews")).json()
        detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    item = next(i for i in page["items"] if i["run_id"] == run_id)
    assert detail["origin"] == ORIGIN_SCHEDULED
    assert detail["auto_review"] == item["auto_review"]
    assert detail["auto_review"]["reasons"] == ["与已发内容高度雷同。"]


async def test_run_detail_has_no_auto_block_for_a_manual_run(session_factory):
    """人工 Run 的详情里 `auto_review` 是 `None`——**不是**空对象、不是全 False 的壳。

    全 False 的壳会让界面显示一个"自动审阅：未通过"的假结论；而真实情况是**没有
    自动审阅者看过它**。这两种情况在翻正门槛里是不同的分母。
    """
    run_id = await _make_run(session_factory, "人手发起的题")

    async with admin_api(session_factory) as api:
        detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    assert detail["origin"] == "manual"
    assert detail["auto_review"] is None


async def test_run_list_marks_which_runs_the_scheduler_created(session_factory):
    """Run 列表要能一眼分出"机器自己排的"和"人发起的"。

    当日额度、影子一致率的分母、reaper 该收谁——三件事都靠这个字段界定范围，
    而它们全都发生在**没有队列条目**的 Run 上（比如已经发布的、被收走的）。
    所以它必须在列表里，不能只在待审队列里。

    也钉住默认值：既有代码路径一行没改，建出来的新旧 Run 都必须是 manual。
    """
    auto_id, _ = await _make_auto_waiting_run(session_factory, "自动的题")
    manual_id = await _make_run(session_factory, "人手的题")

    async with admin_api(session_factory) as api:
        page = (await api.get("/api/admin/runs?page_size=100")).json()

    by_id = {i["run_id"]: i for i in page["items"]}
    assert by_id[auto_id]["origin"] == ORIGIN_SCHEDULED
    assert by_id[manual_id]["origin"] == "manual"


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


async def test_analytics_distributions(session_factory):
    # 三种审核决定各一个
    await _make_reviewed_run(session_factory, "批准", action="approve")
    await _make_reviewed_run(session_factory, "改写", action="edit")
    reject_run = await _make_run(session_factory, "拒绝")
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, reject_run)
        await claim_run_for_review(session, reject_run, expected="waiting_for_review", new_status="rejected")
    async with session_factory() as session:
        draft_id = await create_draft(
            session, reject_run, content="被拒的稿", version=1, prompt_version="v1", model_name="fake"
        )
    async with session_factory() as session:
        await create_review(
            session, draft_id=draft_id, action="reject", original_content="被拒的稿", reject_reason="不符"
        )

    published_run, published_draft = await _make_reviewed_run(session_factory, "发布成功")
    await _publish_as(session_factory, published_run, published_draft, "published")
    failed_run, failed_draft = await _make_reviewed_run(session_factory, "发布失败")
    await _publish_as(session_factory, failed_run, failed_draft, "failed")

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/analytics")).json()

    # approve 是 3 而不是 1：发布成功/失败那两条 Run 也是经 approve 审核的，
    # 审核决定与发布结局是两条独立的轴，不该互相抵消。
    assert data["review_decisions"] == {"approve": 3, "edit": 1, "reject": 1, "other": 0}
    assert data["publish_outcomes"]["published"] == 1
    assert data["publish_outcomes"]["failed"] == 1
    assert data["publish_outcomes"]["publish_unknown"] == 0
    assert data["publish_outcomes"]["publishing"] == 0
    # 口径说明随数据下发，前端不用自己编一句话
    assert "per Run" in data["publish_outcomes"]["note"]

    # 主题分布按次数倒序
    topics = {row["topic"]: row["count"] for row in data["topic_distribution"]}
    assert topics["发布成功"] == 1
    assert data["run_volume"] == 5
    assert len(data["daily_runs"]) == 7


async def test_analytics_counts_human_decisions_only(session_factory):
    """那块分布标着"一次人工决定一行"，就不能把机器的决定也数进去。

    正式模式下自动侧会往 `review` 表写一行 `decided_by='auto'` 的批准
    （见 `app/services/auto_publish.py::_publish`）。它和人的决定**共用同一张表、
    同一列状态**，所以"数 review 表"这件事默认会把两者混成一个数——界面上却写着
    "人工决定"。这是最容易被忽略的一类污染：数字本身没错，错的是它回答的问题。

    这里不要自动 Run、也不要发布，只造一条机器决定，把"分布只认人"这件事
    单独钉住。
    """
    await _make_reviewed_run(session_factory, "人批准的")

    async with session_factory() as session:
        draft_id = await create_draft(
            session, await _make_run(session_factory, "机器批准的"),
            content="草稿正文", version=1, prompt_version="v1", model_name="fake",
        )
    async with session_factory() as session:
        await create_review(
            session,
            draft_id=draft_id,
            action="approve",
            original_content="草稿正文",
            decided_by=DECIDED_BY_AUTO,
        )

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/analytics")).json()

    # 表里有两行 approve，但只有一行是**人**做的决定。
    assert data["review_decisions"]["approve"] == 1


async def test_analytics_survives_a_run_without_a_topic(session_factory):
    """topic 为 NULL 的 Run 不能把整个 analytics 打挂。

    这是真实复现过的 500：Phase 5A 的 Agent 链路在选定主题**之前**就创建 Run
    （`create_agent_run(goal=..., topic=NULL)`），于是库里一定存在 topic 为 NULL
    的行；`topic_distribution` 按 `AgentRun.topic` 分组时把它们也带了出来，落到
    `TopicCount(topic=None)` 上——而那个字段是 `str`，Pydantic 直接抛
    ValidationError，整个 `/api/admin/analytics` 变成 500。

    只要有一个正在选题的 Run 在库里，控制台的 Analytics 页就会整页不可用，
    所以这里钉两层：HTTP 200，以及 NULL **不能**以任何形式出现在分布里。
    最后一条同时排掉了"用 'unknown' 之类的占位值蒙混过去"这种修法——
    那会把一个运行中的临时状态说成一种内容主题。
    """
    await _make_goal_run(session_factory, goal="还没选主题的目标")
    await _make_run(session_factory, "有主题的题")

    async with admin_api(session_factory) as api:
        response = await api.get("/api/admin/analytics")

    assert response.status_code == 200
    data = response.json()
    assert [row["topic"] for row in data["topic_distribution"]] == ["有主题的题"]
    # 排除的是"这张图里的这一条"，不是这个 Run：它照样计入总量。
    assert data["run_volume"] == 2


async def test_analytics_review_other_bucket_absorbs_unknown_statuses(session_factory):
    """三个已知决定之外的 `review.status` 必须落进 other。

    控制台的分布是拿 other 兜底的，而且界面上明说了"四个数字相加等于审核记录总数"。
    真出现一个没见过的状态时，这里要是把它漏掉，四项之和就会小于总数，界面那句话
    当场变成假话。所以这个桶的行为单独钉一下：pending 与一个完全陌生的值都算 other。
    """
    await _make_reviewed_run(session_factory, "正常批准")
    pending_run, _ = await _make_reviewed_run(session_factory, "停在待审")
    stranger_run, _ = await _make_reviewed_run(session_factory, "意外的状态")

    async with session_factory() as session:
        for run_id, status in ((pending_run, "pending"), (stranger_run, "superseded")):
            await session.execute(
                text(
                    "UPDATE review SET status=:s WHERE draft_id IN "
                    "(SELECT id FROM draft WHERE agent_run_id=:r)"
                ),
                {"s": status, "r": uuid.UUID(run_id)},
            )
        await session.commit()

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/analytics")).json()

    assert data["review_decisions"] == {"approve": 1, "edit": 0, "reject": 0, "other": 2}


async def test_analytics_duration_reports_sample_size(session_factory):
    """平均耗时只能覆盖终态 Run，必须如实给出样本数与排除数。"""
    # 一个终态 Run：跑了 120 秒
    done = await _make_run(session_factory, "跑完的题")
    async with session_factory() as session:
        started = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
        await session.execute(
            text(
                "UPDATE agent_run SET status='completed', started_at=:s, finished_at=:f "
                "WHERE id=:id"
            ),
            {"s": started, "f": started + timedelta(seconds=120), "id": uuid.UUID(done)},
        )
        await session.commit()

    # 两个非终态 Run（approved / waiting_for_review）：finished_at 必须为空
    await _make_reviewed_run(session_factory, "待发布的题")
    waiting = await _make_run(session_factory, "等待审核的题")
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, waiting)

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/analytics")).json()

    duration = data["average_run_duration"]
    assert duration["available"] is True
    assert duration["average_seconds"] == 120.0
    assert duration["sampled_runs"] == 1
    assert duration["total_runs"] == 3  # 明确告诉读图的人：只占 1/3
    assert duration["reason"] is None


async def test_analytics_duration_unavailable_instead_of_zero(session_factory):
    """没有任何终态 Run 时，指标标记为不可用并给出原因 —— 不能返回 0。"""
    await _make_reviewed_run(session_factory, "还没发布")
    waiting = await _make_run(session_factory, "等待审核")
    async with session_factory() as session:
        await mark_run_waiting_for_review(session, waiting)

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/analytics")).json()

    duration = data["average_run_duration"]
    assert duration["available"] is False
    assert duration["average_seconds"] is None
    assert duration["sampled_runs"] == 0
    assert "finished_at" in duration["reason"]


async def test_analytics_marks_x_performance_unavailable(session_factory):
    """**没采过**时标为不可用，绝不用 0 冒充。

    Phase 5B.4 起这句话的含义变了：从前是"尚未实现"，现在是"还没有采过"
    （采集脚本已经落地）。所以断言从"提到未实现"改成"提到还没采集"——
    要点没变：**没有数据时必须给出原因，而不是给一个 0**。
    """
    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/analytics")).json()

    performance = data["x_content_performance"]
    assert performance["available"] is False
    assert "have been collected" in performance["reason"]
    # 响应里不得出现任何伪造的 X 指标字段
    for fake in ("impressions", "engagement_rate", "bookmarks"):
        assert fake not in data


async def test_analytics_daily_buckets_follow_utc_days(session_factory):
    """日分桶按 UTC 自然日，且稀疏数据要补 0。"""
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    first = await _make_run(session_factory, "今天第一条")
    await _set_created_at(session_factory, first, today)
    second = await _make_run(session_factory, "今天第二条")
    await _set_created_at(session_factory, second, today + timedelta(minutes=30))

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/analytics?days=3")).json()

    assert len(data["daily_runs"]) == 3
    assert data["daily_runs"][-1] == {"date": today.date().isoformat(), "count": 2}
    assert data["daily_runs"][0]["count"] == 0  # 没数据的天补 0


async def test_analytics_window_days_is_validated(session_factory):
    async with admin_api(session_factory) as api:
        assert (await api.get("/api/admin/analytics?days=0")).status_code == 422
        assert (await api.get("/api/admin/analytics?days=9999")).status_code == 422


# ---------------------------------------------------------------------------
# Published
# ---------------------------------------------------------------------------


async def test_published_list_includes_unresolved_rows(session_factory):
    """发布记录列表不得藏起失败与未知的行——那正是要盯着看的。"""
    ok_run, ok_draft = await _make_reviewed_run(session_factory, "成功的题")
    await _publish_as(session_factory, ok_run, ok_draft, "published")
    fail_run, fail_draft = await _make_reviewed_run(session_factory, "失败的题")
    await _publish_as(session_factory, fail_run, fail_draft, "failed")
    unknown_run, unknown_draft = await _make_reviewed_run(session_factory, "未知的题")
    await _publish_as(session_factory, unknown_run, unknown_draft, "publish_unknown")

    async with admin_api(session_factory) as api:
        everything = (await api.get("/api/admin/published")).json()
        only_published = (await api.get("/api/admin/published?status=published")).json()

    assert everything["page"]["total"] == 3
    assert {i["status"] for i in everything["items"]} == {"published", "failed", "publish_unknown"}
    assert [i["status"] for i in only_published["items"]] == ["published"]

    by_run = {i["run_id"]: i for i in everything["items"]}
    assert by_run[ok_run]["x_post_id"] == "1234567890"
    assert by_run[ok_run]["published_at"] is not None
    assert by_run[ok_run]["topic"] == "成功的题"
    assert by_run[fail_run]["x_post_id"] is None
    assert by_run[fail_run]["last_error"] == "X 明确拒绝"


async def test_published_unresolved_filter_orders_by_urgency(session_factory):
    """`unresolved=true` 只留待裁决的两态，并且 publish_unknown 排在最前。

    排序不是装饰：publish_unknown 意味着"一条推文可能已经发出去了却没人知道"，
    它是四种状态里唯一会随时间变得更糟的（放着不管，重复发帖的风险就一直挂着）。
    publishing 次之——可能还在飞行中，结论要等一会儿才明确。
    """
    ok_run, ok_draft = await _make_reviewed_run(session_factory, "成功的题")
    await _publish_as(session_factory, ok_run, ok_draft, "published")
    fail_run, fail_draft = await _make_reviewed_run(session_factory, "失败的题")
    await _publish_as(session_factory, fail_run, fail_draft, "failed")

    # 顺序是刻意反着来的：**先造 publish_unknown、后造 publishing**。
    # 这样"按创建时间倒序"（publishing 在前）与"按紧急度"（publish_unknown 在前）
    # 给出相反的结果——若把优先级排序删掉，这条测试会立刻红。
    # 反过来造的话两种排序恰好一致，测试就变成了摆设。
    unknown_run, unknown_draft = await _make_reviewed_run(session_factory, "未知的题")
    await _publish_as(session_factory, unknown_run, unknown_draft, "publish_unknown")
    pub_run, pub_draft = await _make_reviewed_run(session_factory, "在途的题")
    await _publish_as(session_factory, pub_run, pub_draft, "publishing")

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/published?unresolved=true")).json()

    assert data["page"]["total"] == 2
    assert [i["status"] for i in data["items"]] == ["publish_unknown", "publishing"]
    assert [i["run_id"] for i in data["items"]] == [unknown_run, pub_run]


async def test_published_unresolved_filter_intersects_with_status(session_factory):
    """`unresolved` 与 `status` 是取交集，不是互相覆盖。

    传入互斥的组合（published 且 unresolved）应当得到空页——两个条件都是"再收窄
    一层"。若实现让其中一个悄悄失效，调用方会拿到一份自己没要的数据。
    """
    ok_run, ok_draft = await _make_reviewed_run(session_factory, "成功的题")
    await _publish_as(session_factory, ok_run, ok_draft, "published")
    unknown_run, unknown_draft = await _make_reviewed_run(session_factory, "未知的题")
    await _publish_as(session_factory, unknown_run, unknown_draft, "publish_unknown")

    async with admin_api(session_factory) as api:
        both_ok = (
            await api.get("/api/admin/published?status=publish_unknown&unresolved=true")
        ).json()
        contradictory = (
            await api.get("/api/admin/published?status=published&unresolved=true")
        ).json()

    assert [i["run_id"] for i in both_ok["items"]] == [unknown_run]
    assert contradictory["items"] == []
    assert contradictory["page"]["total"] == 0


async def test_published_date_range_is_half_open_and_intersects_with_unresolved(session_factory):
    """日期窗口同样左闭右开，并且与 unresolved 取交集。

    控制台的 From / To 与"Needs a decision"是可以同时生效的两个筛选（界面上就在
    同一行），所以这里要验证的是**两个条件都成立**才留下——把日期条件写丢或写成
    并集，都会让"最近的待裁决"变成一个覆盖全部历史的列表。

    三行的 created_at 刻意这样排：B 在右端点上（属于窗口外），C 比 A 新。
    这样既钉住半开区间，也钉住"最新的在前"。
    """
    a_run, a_draft = await _make_reviewed_run(session_factory, "窗口内的待裁决")
    await _publish_as(session_factory, a_run, a_draft, "publish_unknown")
    await _set_publish_created_at(
        session_factory, a_run, datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    )

    b_run, b_draft = await _make_reviewed_run(session_factory, "正好在右端点")
    await _publish_as(session_factory, b_run, b_draft, "publish_unknown")
    await _set_publish_created_at(
        session_factory, b_run, datetime(2026, 9, 11, 0, tzinfo=timezone.utc)
    )

    c_run, c_draft = await _make_reviewed_run(session_factory, "窗口内的确定结局")
    await _publish_as(session_factory, c_run, c_draft, "failed")
    await _set_publish_created_at(
        session_factory, c_run, datetime(2026, 9, 10, 13, tzinfo=timezone.utc)
    )

    window = "date_from=2026-09-10T00:00:00Z&date_to=2026-09-11T00:00:00Z"
    async with admin_api(session_factory) as api:
        in_window = (await api.get(f"/api/admin/published?{window}")).json()
        unresolved_in_window = (await api.get(f"/api/admin/published?{window}&unresolved=true")).json()

    # 右端点上的 B 不在窗口里；窗口内按最新的在前
    assert [i["run_id"] for i in in_window["items"]] == [c_run, a_run]
    assert in_window["page"]["total"] == 2

    # 两个条件都生效：C 在窗口内但不是待裁决，B 是待裁决但不在窗口内
    assert [i["run_id"] for i in unresolved_in_window["items"]] == [a_run]
    assert unresolved_in_window["page"]["total"] == 1


async def test_published_empty_state(session_factory):
    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/published")).json()

    assert data["items"] == []
    assert data["page"] == {"page": 1, "page_size": 20, "total": 0, "pages": 0}


# ---------------------------------------------------------------------------
# 结构性约束
# ---------------------------------------------------------------------------


#: 控制台上**允许**存在的写端点。这是一张**封闭白名单**：往控制台加任何一条写路径
#: 都会让下面那条测试红，逼着人回来重新想一遍——它守的是架构边界，不是某个功能。
#:
#: 有两条。它们有一条共同的性质：**都不实现自己那套运行状态迁移**，
#: 而是去调既有的那条路径。
#:
#:     POST /api/admin/learning/proposals/{proposal_id}/review
#:     POST /api/admin/auto-publish/tick
#:
#: 第一条记的是**人对一条学习提案的裁决**（Phase 5D）。这个动作在 Phase 3/4a 的
#: 写路径里没有对应物——它不是"改变某次运行"，是"记下某次决定"。写下之后
#: 什么都不发生：不改 Prompt / StyleRecipe / 权重 / 阈值 / 预算，也不发布。
#:
#: 第二条（无人值守自动发布）比第一条重得多，所以它单独需要令牌
#: （`AUTO_PUBLISH_TICK_TOKEN`，不配就是关着的）。但它**没有**违反上面那条边界：
#: tick 内部的建 Run / resume / 发布走的全是 `create_agent_run`、
#: `resume_review`、`PublishService` —— 人工路径上的**同一个函数**，
#: 一行私有的状态迁移都没有。它在控制台上表现为一个写端点，只是因为它是个
#: 需要人来按的扳机；被触发的那个动作仍然住在 Phase 3/4a 的路径里。
#:
#: **真正改变运行状态的迁移路径仍然只有 Phase 3/4a 那三条**，前端必须继续复用
#: 它们（review / publish / publish-resolve），否则就会绕过 Phase 4a 用条件
#: UPDATE 换来的并发与幂等保护。
ADMIN_ALLOWED_WRITE_PATHS = {
    "/api/admin/learning/proposals/{proposal_id}/review",
    "/api/admin/auto-publish/tick",
}


async def test_admin_api_writes_are_limited_to_the_proposal_review(session_factory):
    """控制台的写端点只有"记一条裁决"，**没有**任何 Run 状态迁移。

    这条测试守的是架构边界，不是某个功能。它有三层：

    1. `/api/admin` 下**只有**白名单里那一条路径不是纯 GET；
    2. 那条路径只暴露 POST（不是 PUT / PATCH / DELETE——裁决是**追加**，
       不是改写，DELETE 一个审计记录这件事本身就不该存在）；
    3. 任何管理端写路径都**不许**落在 `/api/runs/` 下：那里是运行状态迁移，
       控制台一旦长出自己的第二套，就会漏掉条件 UPDATE 的并发与幂等保护。
    """
    from app.main import app

    schema = app.openapi()
    admin_paths = {
        path: ops for path, ops in schema["paths"].items() if path.startswith("/api/admin")
    }
    assert admin_paths, "admin routes are not mounted"

    writes = {
        path: {method for method in operations if method != "get"}
        for path, operations in admin_paths.items()
        if set(operations) != {"get"}
    }
    assert set(writes) == ADMIN_ALLOWED_WRITE_PATHS, (
        "控制台的写端点与白名单不一致；新增写路径前请先想清楚它改的是什么："
        f"{sorted(set(writes) ^ ADMIN_ALLOWED_WRITE_PATHS)}"
    )
    for path, methods in writes.items():
        assert methods == {"post"}, f"{path} exposes unexpected write methods: {methods}"

    for path in ADMIN_ALLOWED_WRITE_PATHS:
        assert "/api/runs/" not in path, (
            f"{path} 是 Run 状态迁移路径；控制台不得新增这类写端点"
        )
