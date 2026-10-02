"""反馈接入 Writer 的端到端行为（Phase 5B.4）。

单测各自的模块都证明不了的是这四条**跨模块**承诺：

1. 库里有快照时，反馈真的进了 state 并且**只**到了 Writer 的 prompt 里；
2. 库里没有快照时，整条链路照常跑完，`{{FEEDBACK}}` 是一段交代清楚的文本
   （不是空占位符，也不是空白）；
3. 反馈**不改变**这次运行的任何既有结论——trend 分、步数、预算都不动
   （工作单第二十七、三十九节）；
4. 从 checkpoint 读回来的 `feedback_context` **还是对象**，信号种类还是枚举
   （SERDE 登记，见 `app/db/checkpointer.py`）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.feedback.schema import FeedbackContext, FeedbackSignalKind
from app.models import Draft, PostMetricsSnapshot, PublishedPost
from app.models.published_post import STATUS_PUBLISHED
from app.repositories import create_agent_run
from support import runtime_with_checkpointer
from test_agent_loop import config_for, drive, happy_llm
from test_style_writer_integration import prompts_by_kind

PUBLISHED_AT = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)


async def seed_published(session_factory, *, posts: int = 1, snapshots_each: int = 5, likes: int = 10):
    """造"我们发过东西、也采到了结果"的存量。

    刻意走真实的表而不是替身：这一层要证明的正是"库里的快照能到达 prompt"，
    用假数据源等于把被测的那一段整个跳过。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        for p in range(posts):
            draft = Draft(agent_run_id=run.id, content="已发布的正文", status="published", version=1)
            session.add(draft)
            await session.flush()
            post = PublishedPost(
                draft_id=draft.id,
                agent_run_id=run.id,
                body="已发布的正文",
                status=STATUS_PUBLISHED,
                x_post_id=f"190000000000000000{p}",
                published_at=PUBLISHED_AT,
            )
            session.add(post)
            await session.flush()
            for i in range(snapshots_each):
                hours = float(i + 1)
                session.add(
                    PostMetricsSnapshot(
                        published_post_id=post.id,
                        x_post_id=post.x_post_id,
                        observed_at=PUBLISHED_AT + timedelta(hours=hours),
                        hours_since_publish=hours,
                        likes=likes + i,
                        replies=2,
                        reposts=1,
                        quotes=1,
                    )
                )
        await session.commit()


# ---------------------------------------------------------------------------
# 有快照：进 state，且只到 Writer
# ---------------------------------------------------------------------------


async def test_observed_metrics_reach_the_writer_prompt(session_factory):
    await seed_published(session_factory, snapshots_each=5)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

        context = out["feedback_context"]
        assert isinstance(context, FeedbackContext)
        assert context.snapshot_count == 5
        assert context.post_count == 1
        assert not context.is_empty
        assert context.signals
        assert all(s.evidence for s in context.signals)

        writer_prompt = prompts_by_kind(llm)["candidates"]
        # "样本：5 条观察…"这句只可能来自上下文本身（模板里没有它）。
        assert "5 条观察" in writer_prompt
        assert "观察到的分布" in writer_prompt
        # 观察句进来了，但**没有**变成指令
        assert "不是本次的写作要求" in writer_prompt


async def test_feedback_never_reaches_the_critic(session_factory):
    """Critic 不读反馈：评审"稿子好不好"不该被"发出去效果好不好"污染。"""
    await seed_published(session_factory, snapshots_each=5)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    critic_prompt = prompts_by_kind(llm)["critic"]
    assert "5 条观察" not in critic_prompt
    assert "不是本次的写作要求" not in critic_prompt
    # Critic 那一轮 state 里的事件仍在，但它没有被渲染进 prompt
    assert out["feedback_context"].snapshot_count == 5


async def test_the_feedback_block_contains_no_post_bodies(session_factory):
    """工作单第四十二节：Writer 拿到的是分布，不是"这种帖子拿了多少赞"。"""
    await seed_published(session_factory, snapshots_each=5)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    body = out["feedback_context"]
    rendered = "\n".join(s.statement for s in body.signals)
    for signal in body.signals:
        assert signal.comparison_group is None
        assert signal.confidence is None
    # 逐条帖子的数字（"14 个赞"这种）不会以单条形式出现
    assert "已发布的正文" not in rendered


# ---------------------------------------------------------------------------
# 没有快照：照常跑完，且占位符被替换
# ---------------------------------------------------------------------------


async def test_a_run_without_any_snapshot_still_completes(session_factory):
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

        assert "__interrupt__" in out
        assert out["draft_content"]

        context = out["feedback_context"]
        assert isinstance(context, FeedbackContext)
        assert context.is_empty
        assert context.signals == ()

        writer_prompt = prompts_by_kind(llm)["candidates"]
        # 占位符被替换掉了，而且换上的是一段**说明**，不是空白
        assert "{{" not in writer_prompt
        assert "还没有被采集过" in writer_prompt


# ---------------------------------------------------------------------------
# 反馈不改变本次运行的任何既有结论
# ---------------------------------------------------------------------------


async def test_feedback_does_not_move_the_trend_scores(session_factory):
    """工作单第二十七节：反馈不得改写当前运行的 Trend 评分。

    跑两次（有快照 / 无快照）、同一份目标与材料，trend 分必须逐条相同——
    只比一个总分是不够的：反馈若只影响了某一条的子分，总分也可能碰巧不变。
    """
    llm_a = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm_a) as runtime:
        _, without = await drive(runtime, session_factory, llm_a)

    await seed_published(session_factory, snapshots_each=5)
    llm_b = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm_b) as runtime:
        _, with_feedback = await drive(runtime, session_factory, llm_b)

    assert with_feedback["feedback_context"].snapshot_count == 5
    assert without["feedback_context"].is_empty

    def scores(state):
        return [
            (t.title, t.trend_score) for t in state["candidate_topics"]
        ]

    assert scores(with_feedback) == scores(without)


async def test_feedback_does_not_change_the_budget_or_step_count(session_factory):
    """工作单第三十九节：反馈不是新的一步，也不动预算。

    步数与预算计数必须与没有反馈时**逐位相同**。这里同时钉住了"没有新增节点"：
    多一个节点会多一次 planner/tool 往返，`step_count` 会立刻变大。
    """
    llm_a = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm_a) as runtime:
        _, without = await drive(runtime, session_factory, llm_a)

    await seed_published(session_factory, snapshots_each=5)
    llm_b = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm_b) as runtime:
        _, with_feedback = await drive(runtime, session_factory, llm_b)

    # 这几个计数是 total=False 的可选键：这条路径上没有改稿，`revision_count`
    # 根本没被写过。缺省按 0 比——但要在两侧都缺省，否则比的是"有没有这个键"。
    for key in ("step_count", "research_count", "revision_count"):
        assert with_feedback.get(key, 0) == without.get(key, 0)
    assert llm_a.planner_calls() == llm_b.planner_calls()


# ---------------------------------------------------------------------------
# checkpoint 往返
# ---------------------------------------------------------------------------


async def test_feedback_context_survives_the_checkpoint_as_objects(session_factory):
    """读回来的信号必须还是 `FeedbackSignal`，`kind` 必须还是枚举。

    这条钉的是 SERDE 登记（同 5B.2 的 recipe 往返测试）：未登记的枚举会被
    拦成普通字符串，随后 pydantic 在类型化字段上又把它校验回枚举——
    看起来完全正确、断言全绿、只有日志在响。所以这里断言**类型**。
    """
    await seed_published(session_factory, snapshots_each=5)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, _ = await drive(runtime, session_factory, llm)
        snapshot = await runtime.graph.aget_state(config_for(run_id))

    context = snapshot.values["feedback_context"]
    assert isinstance(context, FeedbackContext)
    assert context.signals
    assert all(isinstance(s.kind, FeedbackSignalKind) for s in context.signals)
    assert any(s.kind is FeedbackSignalKind.ATTENTION for s in context.signals)
