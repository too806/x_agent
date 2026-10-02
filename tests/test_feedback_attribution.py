"""归因：结果 + 交付 + 当时决策并排（`app/feedback/profile.py`）。

这一层测的核心不是"字段读得对不对"，而是**读不到的时候它怎么表现**：

- checkpoint 被回收 / 读失败 → 退化成只有数据库那部分 + 一条局限说明，
  而不是抛异常、也不是补一个默认值；
- 观察超过读取上限 → 如实说出"只读了最近 N 条"。

一个补了默认值的归因看起来比一个说了"缺一半"的归因完整得多——
而它会把"当时 trend 分是 0"这种从未发生过的结论写进报告。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.feedback.profile import (
    FEEDBACK_PROFILE_READ_LIMIT,
    build_post_performance_profile,
)
from app.models import PostMetricsSnapshot, PublishedPost
from app.models.draft import Draft
from app.models.published_post import STATUS_PUBLISHED
from app.repositories import create_agent_run

PUBLISHED_AT = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


class FailingGraph:
    """checkpoint 读不到（被回收 / 图还在写）。"""

    async def aget_state(self, config):
        raise RuntimeError("checkpoint gone")


class EmptyGraph:
    async def aget_state(self, config):
        return SimpleNamespace(values={})


async def seed_post_with_snapshots(session_factory, *, snapshots: int = 3, body: str = "正文" * 10):
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        draft = Draft(agent_run_id=run.id, content=body, status="published", version=1)
        session.add(draft)
        await session.flush()
        post = PublishedPost(
            draft_id=draft.id,
            agent_run_id=run.id,
            body=body,
            status=STATUS_PUBLISHED,
            x_post_id="1900",
            published_at=PUBLISHED_AT,
        )
        session.add(post)
        await session.flush()
        for i in range(snapshots):
            session.add(
                PostMetricsSnapshot(
                    published_post_id=post.id,
                    x_post_id="1900",
                    observed_at=PUBLISHED_AT + timedelta(hours=i + 1),
                    hours_since_publish=float(i + 1),
                    likes=10 + i,
                    replies=2,
                    reposts=1,
                    quotes=1,
                )
            )
        await session.commit()
        return str(post.id), str(run.id)


def state_with(*, topic: str = "AI coding agent"):
    """一份像"当时真的跑过"的 checkpoint state。"""
    return {
        "selected_topic": topic,
        "candidate_topics": [
            SimpleNamespace(title="别的题", trend_score=40.0),
            SimpleNamespace(title=topic, trend_score=76.5),
        ],
        "selected_angle": SimpleNamespace(claim="定价权在转移", confidence=0.8),
        "style_recipe": SimpleNamespace(
            structure_type=SimpleNamespace(value="opinion_first"),
            hook_type=SimpleNamespace(value="question"),
            effective_target_length=140,
        ),
        "critic_result": SimpleNamespace(score=8.0),
    }


class StubGraph:
    def __init__(self, values):
        self._values = values

    async def aget_state(self, config):
        return SimpleNamespace(values=self._values)


async def test_profile_joins_result_delivery_and_decision(session_factory):
    post_id, run_id = await seed_post_with_snapshots(session_factory, snapshots=3)

    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, StubGraph(state_with()), published_post_id=post_id
        )

    assert profile is not None
    assert profile.x_post_id == "1900"
    assert profile.published_at == PUBLISHED_AT
    assert profile.run_id == run_id
    assert profile.content_length == len("正文" * 10)
    # 结果：观察序列，时间正序
    assert [o.likes for o in profile.observations] == [10, 11, 12]
    assert [o.hours_since_publish for o in profile.observations] == [1.0, 2.0, 3.0]
    # 决策：当时记下来的，不是现在重算的
    assert profile.selected_topic == "AI coding agent"
    assert profile.topic_trend_score == 76.5  # 按 title 对上，而不是取第一条
    assert profile.selected_angle_claim == "定价权在转移"
    assert profile.style_structure == "opinion_first"
    assert profile.style_target_length == 140
    assert profile.critic_score == 8.0
    assert profile.limitations == ()


async def test_unknown_post_is_none_not_an_empty_profile(session_factory):
    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, StubGraph({}), published_post_id="00000000-0000-0000-0000-000000000000"
        )
    assert profile is None


async def test_a_dead_checkpoint_degrades_instead_of_failing(session_factory):
    """读不到 checkpoint → 数据库那部分照给，缺口写成一句看得见的话。"""
    post_id, _ = await seed_post_with_snapshots(session_factory, snapshots=2)

    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, FailingGraph(), published_post_id=post_id
        )

    assert profile is not None
    assert len(profile.observations) == 2  # 结果部分完好
    # 决策部分**不猜**：全是 None，而不是 0
    assert profile.selected_topic is None
    assert profile.topic_trend_score is None
    assert profile.style_structure is None
    assert profile.critic_score is None
    assert any("checkpoint" in note for note in profile.limitations)


async def test_an_empty_checkpoint_is_treated_as_unavailable(session_factory):
    post_id, _ = await seed_post_with_snapshots(session_factory, snapshots=1)

    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, EmptyGraph(), published_post_id=post_id
        )

    assert profile.selected_topic is None
    assert any("为空" in note for note in profile.limitations)


async def test_an_unmatched_selected_topic_leaves_the_score_unknown(session_factory):
    """选中的题目在候选集里找不到 → `None`，**绝不退回第一条的分数**。"""
    post_id, _ = await seed_post_with_snapshots(session_factory, snapshots=1)
    state = state_with()
    # 候选集里没有这个标题（正常链路不会发生，但 checkpoint 与库不同步时就会）
    state["selected_topic"] = "候选集里没有的题"

    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, StubGraph(state), published_post_id=post_id
        )

    assert profile.selected_topic == "候选集里没有的题"
    assert profile.topic_trend_score is None


async def test_a_post_with_no_snapshots_says_so(session_factory):
    post_id, _ = await seed_post_with_snapshots(session_factory, snapshots=0)

    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, StubGraph(state_with()), published_post_id=post_id
        )

    assert profile.observations == ()
    assert any("还没有任何观察快照" in note for note in profile.limitations)


async def test_the_read_cap_is_stated_not_silently_applied(session_factory):
    post_id, _ = await seed_post_with_snapshots(
        session_factory, snapshots=FEEDBACK_PROFILE_READ_LIMIT + 1
    )

    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, StubGraph(state_with()), published_post_id=post_id
        )

    assert len(profile.observations) == FEEDBACK_PROFILE_READ_LIMIT
    assert any(str(FEEDBACK_PROFILE_READ_LIMIT) in note for note in profile.limitations)


async def test_topic_comes_from_the_run_row_not_the_checkpoint(session_factory):
    """`topic` 是库里那条运行记录的字段，不是从 checkpoint 拼的。

    两条来源**分别**降级：checkpoint 死了，`topic` 仍然在（它也正因为如此，
    即使归因缺了决策部分，读者仍知道这条帖子是哪个题目的）。
    """
    post_id, run_id = await seed_post_with_snapshots(session_factory, snapshots=1)

    async with session_factory() as session:
        profile = await build_post_performance_profile(
            session, FailingGraph(), published_post_id=post_id
        )

    assert profile.topic == "AI coding agent"
    assert profile.run_id == run_id
