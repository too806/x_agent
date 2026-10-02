"""`post_metrics_snapshot` 的读写语义（Phase 5B.4）。

表本身很简单，但有两件事必须钉死：

1. **NULL 被原样保存**。写入路径上任何一处 `or 0` 都会让"X 没返回这个字段"
   变成"零个互动"，而那是本阶段口径上最严重的一类错误。
2. **重复抓同一时刻是幂等的**。抓取脚本会被人工反复跑；同一 (x_post_id,
   observed_at) 不该产生第二行，也不该报错。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.models import PostMetricsSnapshot
from app.models.published_post import (
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    PublishedPost,
)
from app.models.draft import Draft
from app.repositories import (
    count_post_metrics_snapshots,
    create_agent_run,
    get_published_post_by_id,
    insert_post_metrics_snapshot,
    latest_metrics_by_post,
    latest_metrics_observed_at,
    list_post_metrics_snapshots,
    list_published_posts_with_x_id,
    list_recent_post_metrics_snapshots,
)

PUBLISHED_AT = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


async def seed_post(
    session_factory,
    *,
    status: str = STATUS_PUBLISHED,
    x_post_id: str | None = "1900",
    published_at: datetime | None = PUBLISHED_AT,
) -> str:
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        draft = Draft(agent_run_id=run.id, content="正文", status="published", version=1)
        session.add(draft)
        await session.flush()
        post = PublishedPost(
            draft_id=draft.id,
            agent_run_id=run.id,
            body="正文",
            status=status,
            x_post_id=x_post_id,
            published_at=published_at,
        )
        session.add(post)
        await session.commit()
        return str(post.id)


async def _insert(session_factory, post_id, *, observed_at, likes=7, replies=1,
                  reposts=1, quotes=0, hours=2.0):
    async with session_factory() as session:
        return await insert_post_metrics_snapshot(
            session,
            published_post_id=post_id,
            x_post_id="1900",
            observed_at=observed_at,
            hours_since_publish=hours,
            likes=likes,
            replies=replies,
            reposts=reposts,
            quotes=quotes,
        )


async def test_unknown_counts_are_stored_as_null_never_zero(session_factory):
    """未知必须原样落成 NULL——写的时候补 0，读回来就再也分不清了。"""
    post_id = await seed_post(session_factory)
    row = await _insert(session_factory, post_id, observed_at=PUBLISHED_AT, likes=None)

    assert row is not None
    assert row.likes is None
    assert row.impressions is None
    assert row.bookmarks is None
    # 读回来依然是 None
    async with session_factory() as session:
        rows = await list_post_metrics_snapshots(session, published_post_id=row.published_post_id)
    assert rows[0].likes is None


async def test_the_same_instant_is_idempotent(session_factory):
    post_id = await seed_post(session_factory)
    first = await _insert(session_factory, post_id, observed_at=PUBLISHED_AT)
    second = await _insert(session_factory, post_id, observed_at=PUBLISHED_AT, likes=99)

    assert first is not None
    assert second is None  # 冲突 → 跳过，不报错
    async with session_factory() as session:
        assert await count_post_metrics_snapshots(session) == 1
        rows = await list_post_metrics_snapshots(session)
    assert rows[0].likes == 7  # 先到的那条没被改写（只追加，没有更新路径）


async def test_a_different_instant_appends_a_new_row(session_factory):
    post_id = await seed_post(session_factory)
    await _insert(session_factory, post_id, observed_at=PUBLISHED_AT, likes=7)
    await _insert(session_factory, post_id, observed_at=PUBLISHED_AT + timedelta(hours=1), likes=20)

    async with session_factory() as session:
        rows = await list_post_metrics_snapshots(session)
    assert [r.likes for r in rows] == [7, 20]  # 时间正序


async def test_only_confirmed_published_posts_with_an_id_are_listed(session_factory):
    await seed_post(session_factory, status=STATUS_PUBLISHED, x_post_id="1900")
    await seed_post(session_factory, status=STATUS_PUBLISHING, x_post_id="1901")
    await seed_post(session_factory, status=STATUS_FAILED, x_post_id="1902")
    await seed_post(session_factory, status=STATUS_PUBLISHED, x_post_id=None)
    await seed_post(session_factory, status=STATUS_PUBLISHED, published_at=None)

    async with session_factory() as session:
        posts = await list_published_posts_with_x_id(session)
    # 只有第一条：既确认发布、又拿得到 id、也说得清发布时刻
    assert [p.x_post_id for p in posts] == ["1900"]


async def test_recent_snapshots_take_the_newest_but_return_a_timeline(session_factory):
    post_id = await seed_post(session_factory)
    for i in range(5):
        await _insert(
            session_factory,
            post_id,
            observed_at=PUBLISHED_AT + timedelta(hours=i),
            likes=i,
            hours=float(i),
        )

    async with session_factory() as session:
        rows = await list_recent_post_metrics_snapshots(session, limit=2)

    # 取的是**最近**两条，但按正序返回（调用方拿到的是时间线，不是倒序）
    assert [r.likes for r in rows] == [3, 4]


async def test_latest_observed_at_is_none_when_nothing_was_ever_collected(session_factory):
    """从没抓过就是 `None`，**不是 `now()`**——"没采过"与"刚刚采过"是两回事。"""
    async with session_factory() as session:
        assert await latest_metrics_observed_at(session) is None

    post_id = await seed_post(session_factory)
    await _insert(session_factory, post_id, observed_at=PUBLISHED_AT)
    await _insert(
        session_factory, post_id, observed_at=PUBLISHED_AT + timedelta(hours=3), likes=30
    )

    async with session_factory() as session:
        assert await latest_metrics_observed_at(session) == PUBLISHED_AT + timedelta(hours=3)


async def test_get_published_post_by_id(session_factory):
    post_id = await seed_post(session_factory)
    async with session_factory() as session:
        post = await get_published_post_by_id(session, post_id)
        assert post is not None and str(post.id) == post_id
        assert await get_published_post_by_id(session, "00000000-0000-0000-0000-000000000000") is None


async def test_latest_metrics_by_post_takes_one_row_per_post(session_factory):
    """一页帖子的最新观察 + 计数，一次查完；没有快照的帖子**不出现**在结果里。

    不出现是关键：调用方据此渲染「暂无数据」。如果这里返回一个计数为 0 的条目，
    发布列表上每一张没采过的帖子就会显示成"赞 0 回 0"——那是另一句话。
    """
    post_a = await seed_post(session_factory)
    post_b = await seed_post(session_factory)
    post_never_seen = await seed_post(session_factory)

    for hours in (1, 2, 3):
        await _insert(
            session_factory,
            post_a,
            observed_at=PUBLISHED_AT + timedelta(hours=hours),
            likes=10 + hours,
        )
    await _insert(session_factory, post_b, observed_at=PUBLISHED_AT + timedelta(hours=7), likes=99)

    async with session_factory() as session:
        latest = await latest_metrics_by_post(
            session,
            [uuid.UUID(post_a), uuid.UUID(post_b), uuid.UUID(post_never_seen)],
        )

    assert set(latest) == {uuid.UUID(post_a), uuid.UUID(post_b)}
    row_a, count_a = latest[uuid.UUID(post_a)]
    # 最新那条，不是各次之和（13 = 1+2+3 小时那一刻的读数）
    assert row_a.likes == 13
    assert count_a == 3
    row_b, count_b = latest[uuid.UUID(post_b)]
    assert row_b.likes == 99
    assert count_b == 1


async def test_latest_metrics_by_post_with_no_ids_queries_nothing(session_factory):
    """空的帖子列表 → 空字典，且不发出 `IN ()` 这种会报错的 SQL。

    发布列表的最后一页常常是空的，这条路径每次刷新都会走到。
    """
    async with session_factory() as session:
        assert await latest_metrics_by_post(session, []) == {}


async def test_foreign_key_cascade_removes_snapshots_with_the_post(session_factory):
    """帖子的快照随它一起走——没有孤儿行，也就没有"归不到的因"。"""
    post_id = await seed_post(session_factory)
    await _insert(session_factory, post_id, observed_at=PUBLISHED_AT)

    async with session_factory() as session:
        post = await get_published_post_by_id(session, post_id)
        await session.delete(post)
        await session.commit()

    async with session_factory() as session:
        assert await count_post_metrics_snapshots(session) == 0


def test_snapshot_model_has_no_update_path():
    """只追加：模型上不该有任何"更新"用的时间戳列（updated_at）。

    有了它，将来一定会有人写一个 UPDATE 把"当时的观察"改掉，
    而快照的历史价值恰恰在于它永远不会被改写。
    """
    from sqlalchemy import inspect as sa_inspect

    from app.db.base import Base

    columns = {c.name for c in sa_inspect(Base.metadata.tables["post_metrics_snapshot"]).columns}
    assert "updated_at" not in columns
    assert "observed_at" in columns
    assert PostMetricsSnapshot.__tablename__ == "post_metrics_snapshot"
