"""`GET /api/admin/feedback` —— 只读的反馈一览（Phase 5B.4）。

这个接口最容易写坏的地方不是聚合，而是**没有数据时它返回什么**：
返回一串 0 会让"从来没采过"看起来像"采到了，没人互动"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.feedback.report import build_feedback_context, build_report
from app.feedback.schema import SnapshotObservation
from app.models import Draft, PostMetricsSnapshot, PublishedPost
from app.models.published_post import STATUS_PUBLISHED
from app.repositories import create_agent_run
from test_admin_api import admin_api

PUBLISHED_AT = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


async def seed_published(session_factory, *, x_post_id="1900", snapshots=5, likes=10):
    """造一个"已发布 + N 次观察"的帖子，返回 `(帖子 id, Run id)`。

    两个 id 都要：聚合接口按帖子看（帖子 id），Run Detail 按 Run 看（Run id）。
    返回 `(post_id, run_id)` 而不是一个对象，是因为**两个都是 uuid 的字符串形式**，
    用命名对象反而会让人以为它们可以互换——它们不能。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        draft = Draft(agent_run_id=run.id, content="正文", status="published", version=1)
        session.add(draft)
        await session.flush()
        post = PublishedPost(
            draft_id=draft.id,
            agent_run_id=run.id,
            body="正文",
            status=STATUS_PUBLISHED,
            x_post_id=x_post_id,
            published_at=PUBLISHED_AT,
        )
        session.add(post)
        await session.flush()
        for i in range(snapshots):
            session.add(
                PostMetricsSnapshot(
                    published_post_id=post.id,
                    x_post_id=x_post_id,
                    observed_at=PUBLISHED_AT + timedelta(hours=i + 1),
                    hours_since_publish=float(i + 1),
                    likes=likes + i,
                    replies=2,
                    reposts=1,
                    quotes=1,
                )
            )
        await session.commit()
        return str(post.id), str(run.id)


async def test_feedback_without_any_snapshot_reports_a_reason_not_zeros(session_factory):
    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/feedback")).json()

    assert data["available"] is False
    assert data["reason"]
    assert data["sample_count"] == 0
    assert data["signals"] == []
    assert data["posts"] == []
    # 没有数据时不能说"互动是 0"
    assert "impressions" not in data


async def test_feedback_reports_observations_when_they_exist(session_factory):
    await seed_published(session_factory, snapshots=5)

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/feedback")).json()

    assert data["available"] is True
    # 口径说明必须到场：只有公开计数、手工采集、不是质量分
    assert "public counts" in data["reason"]
    assert data["snapshot_count"] == 5
    assert data["post_count"] == 1
    assert data["sample_count"] == 5
    assert data["latest_observed_at"] is not None

    kinds = {signal["kind"] for signal in data["signals"]}
    assert "attention" in kinds
    for signal in data["signals"]:
        # v1 没有基线，也没有校准过的置信度——这两项必须是 null，不能编
        assert signal["comparison_group"] is None
        assert signal["confidence"] is None
        assert signal["evidence"]

    assert data["limitations"]
    assert any("曝光" in note for note in data["limitations"])

    post = data["posts"][0]
    assert post["x_post_id"] == "1900"
    assert post["observation_count"] == 5
    # 取的是**最新**一条观察，不是求和：14 是最后一条的点赞数
    assert post["likes"] == 14
    assert post["hours_since_publish"] == 5.0
    assert post["published_at"] is not None


async def test_unknown_counts_stay_null_over_the_api(session_factory):
    """`None` 必须一路传到 JSON：界面靠它区分"没读到"与"读到 0"。"""
    post_id, _ = await seed_published(session_factory, snapshots=0)
    async with session_factory() as session:
        session.add(
            PostMetricsSnapshot(
                published_post_id=post_id,
                x_post_id="1900",
                observed_at=PUBLISHED_AT + timedelta(hours=1),
                hours_since_publish=1.0,
                likes=None,
                replies=None,
                reposts=None,
                quotes=None,
            )
        )
        await session.commit()

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/feedback")).json()

    post = data["posts"][0]
    assert post["likes"] is None
    assert post["replies"] is None
    assert post["quotes"] is None
    assert post["impressions"] is None


async def test_analytics_flips_only_when_snapshots_exist(session_factory):
    async with admin_api(session_factory) as api:
        before = (await api.get("/api/admin/analytics")).json()
    assert before["x_content_performance"]["available"] is False

    await seed_published(session_factory, snapshots=3)

    async with admin_api(session_factory) as api:
        after = (await api.get("/api/admin/analytics")).json()
    assert after["x_content_performance"]["available"] is True
    assert "manual" in after["x_content_performance"]["reason"]


async def test_the_feedback_endpoint_is_get_only(session_factory):
    """工作单第三十三节：只允许 GET。POST 必须不存在。"""
    async with admin_api(session_factory) as api:
        response = await api.post("/api/admin/feedback", json={})
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Run Detail 里的两块反馈（Phase 5B.4）
#
# 这两块来源不同、时间语义不同，所以在 API 里也是两个字段：
#
#   feedback    = 这次运行**当时读到的**观察摘要（checkpoint）
#   performance = 这条帖子**后来被观察到的**最新一次结果（数据库快照）
#
# 分开的理由不是整洁：快照在这之后还会继续增加，而那次运行看到的是当时那一批。
# 用一个字段承载两者，界面就再也分不出"它当时知道什么"与"现在是什么"。
# ---------------------------------------------------------------------------


async def test_run_detail_reports_no_feedback_for_a_pre_5b4_run(session_factory):
    """键不在 checkpoint 里（5B.4 之前的运行）必须说"没读过"，不是"读到了零"。"""
    _, run_id = await seed_published(session_factory, snapshots=5)

    async with admin_api(session_factory, state={"goal": "找一个值得讨论的 AI 话题"}) as api:
        data = (await api.get(f"/api/admin/runs/{run_id}")).json()

    feedback = data["feedback"]
    assert feedback["available"] is False
    assert "did not read" in feedback["reason"]
    assert feedback["sample_count"] == 0
    assert feedback["signals"] == []
    # 键不在 ≠ 读到了 0 条观察：这两句必须长得不一样
    assert "empty feedback context" not in feedback["reason"]


async def test_run_detail_separates_what_the_run_saw_from_what_the_post_measured(
    session_factory,
):
    """同一个 Run 的两块反馈各自到位，且互不冒充。"""
    _, run_id = await seed_published(session_factory, snapshots=5, likes=10)
    context = build_feedback_context(
        build_report(
            [
                SnapshotObservation(
                    snapshot_id=f"snap-{i}",
                    x_post_id="1900",
                    published_post_id="post",
                    observed_at=PUBLISHED_AT + timedelta(hours=i),
                    hours_since_publish=float(i),
                    likes=10 + i,
                    replies=2,
                    reposts=1,
                    quotes=1,
                )
                for i in range(1, 6)
            ]
        )
    )

    async with admin_api(
        session_factory,
        state={"goal": "找一个值得讨论的 AI 话题", "feedback_context": context},
    ) as api:
        data = (await api.get(f"/api/admin/runs/{run_id}")).json()

    feedback = data["feedback"]
    assert feedback["available"] is True
    assert feedback["sample_count"] == 5
    assert feedback["snapshot_count"] == 5
    assert feedback["source_snapshot_ids"]
    assert feedback["signals"]
    for signal in feedback["signals"]:
        assert signal["statement"]
        assert signal["confidence"] is None
        assert signal["comparison_group"] is None
    assert feedback["limitations"]

    # 另一块：帖子后来被观察到的结果——取最新一条，不是求和也不是那次运行看到的
    performance = data["performance"]
    assert performance["observation_count"] == 5
    assert performance["likes"] == 14
    assert performance["hours_since_publish"] == 5.0
    assert performance["published_at"] is not None


async def test_run_detail_does_not_invent_a_row_for_an_unobserved_post(session_factory):
    """发过帖但一次都没采过：`performance` 是 null，不是一行 0。"""
    _, run_id = await seed_published(session_factory, snapshots=0)

    async with admin_api(session_factory, state={"goal": "找一个值得讨论的 AI 话题"}) as api:
        data = (await api.get(f"/api/admin/runs/{run_id}")).json()

    assert data["performance"] is None
    # 而且不能因为"没有表现数据"就顺手把点赞写成 0
    assert "likes" not in data


# ---------------------------------------------------------------------------
# 发布记录列表里的「最新观察」列（Phase 5B.4）
#
# 这一列是那张表里唯一一个**后补的**观察（其余几列是发布那一刻的审计事实），
# 所以它单独测试两件事：取的是最新一条而不是各次之和；没采过时是 null 不是 0。
# ---------------------------------------------------------------------------


async def test_published_row_reports_the_latest_observation_not_a_sum(session_factory):
    """5 次观察（10/11/12/13/14）→ 显示 14，不是 60，也不是 5。

    `observation_count` 是 5：发布列表一次取一页帖子的最新快照 + 计数，
    那条路径上手里只有一条最新的行，把它当成"观察了 1 次"会漏报采样次数。
    """
    post_id, _ = await seed_published(session_factory, snapshots=5, likes=10)

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/published")).json()

    row = next(item for item in data["items"] if item["publish_id"] == post_id)
    metrics = row["latest_metrics"]
    assert metrics["likes"] == 14
    assert metrics["observation_count"] == 5
    assert metrics["hours_since_publish"] == 5.0
    assert metrics["latest_observed_at"] is not None


async def test_published_row_without_snapshots_reports_null_not_zeros(session_factory):
    """没采过 → `latest_metrics` 是 null（界面显示「暂无数据」），不是一个 0 行。"""
    post_id, _ = await seed_published(session_factory, snapshots=0)

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/published")).json()

    row = next(item for item in data["items"] if item["publish_id"] == post_id)
    assert row["latest_metrics"] is None


async def test_published_list_keeps_unknown_counts_null(session_factory):
    """采到了但 X 没返回某个计数：那个计数是 null，不是 0。"""
    post_id, _ = await seed_published(session_factory, snapshots=0)
    async with session_factory() as session:
        session.add(
            PostMetricsSnapshot(
                published_post_id=post_id,
                x_post_id="1900",
                observed_at=PUBLISHED_AT + timedelta(hours=1),
                hours_since_publish=1.0,
                likes=7,
                replies=None,
                reposts=0,
                quotes=None,
            )
        )
        await session.commit()

    async with admin_api(session_factory) as api:
        data = (await api.get("/api/admin/published")).json()

    metrics = next(i for i in data["items"] if i["publish_id"] == post_id)["latest_metrics"]
    assert metrics["likes"] == 7
    # 三个必须互不相同：没读到（None）/ 读到零（0）/ 读到了（7）
    assert metrics["replies"] is None
    assert metrics["reposts"] == 0
    assert metrics["quotes"] is None
