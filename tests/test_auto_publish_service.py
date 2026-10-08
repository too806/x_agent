"""自动发布 tick 的编排：一次 tick 到底做了什么、没做什么。

这个文件的重点是**否定断言**。"跑通了一轮"是有用的，但真正要钉住的是
每一条"它**没有**做那件事"：

- 影子模式下 `fake_x.created == []` —— 账号上什么都没发；
- 判据不通过时 Run 留在 `waiting_for_review` —— 没有自动拒绝，只是交回给人；
- 配置关掉之后连 Run 都不建 —— 刹车不是"不发布"而是"不动作"；
- 配置改了**不用重启** —— 下一轮 tick 立刻按新值办。

这几个否定一旦失守，失守的方式都是静默的：它会看起来和正常一模一样。
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
import yaml
from sqlalchemy import select

from app.models.agent_run import ORIGIN_SCHEDULED, AgentRun
from app.models.auto_review import MODE_LIVE, MODE_SHADOW, VERDICT_APPROVE, VERDICT_REJECT
from app.models.published_post import STATUS_PUBLISHED, STATUS_UNKNOWN, PublishedPost
from app.repositories import (
    create_agent_run,
    create_draft,
    create_review,
    insert_auto_review,
    release_auto_publish_lock,
    shadow_agreement_stats,
    try_auto_publish_lock,
)
from app.services.auto_review import summarize_agreement
from app.services.auto_publish import (
    OUTCOME_CONFIG_ERROR,
    OUTCOME_DISABLED,
    OUTCOME_LOCKED,
    OUTCOME_REVIEWED,
    OUTCOME_RUN_CAP,
    AutoPublishService,
)
from app.services.x_service import XService
from fakes import FakeXApiClient, candidates_payload
from support import FIXED_NOW, runtime_with_checkpointer
from test_agent_loop import happy_llm

WAITING = "waiting_for_review"


def candidate_content(index: int) -> str:
    """假 LLM 第 index 条候选的正文。

    **从 `fakes.candidates_payload` 现算**而不是抄一份字面量：抄的那份一旦
    和假 LLM 漂开，"制造一条与正文完全相同的已发帖"就再也造不出来了，
    而失败的样子是那条测试**静默地不再测任何东西**。
    """
    payload = json.loads(candidates_payload()(""))
    return payload["candidates"][index - 1]["content"]


def write_config(tmp_path, **overrides) -> str:
    """写一份 automation.yaml，返回路径。

    默认是一份**开着、影子模式、额度宽裕**的配置——每个测试只覆盖它关心的
    那一个字段，这样"这条测试在测什么"一眼看得出来。
    """
    data = {
        "enabled": True,
        "mode": MODE_SHADOW,
        "goals": ["找一个值得讨论的 AI 话题，写一条有明确观点的帖子"],
        "max_auto_runs_per_day": 3,
        "max_publishes_per_day": 1,
        "min_critic_score": 7.0,
        "recent_window_days": 14,
        "recent_corpus_limit": 50,
        "tick_timeout_seconds": 120,
    }
    data.update(overrides)
    path = tmp_path / "automation.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


async def all_runs(session_factory) -> list[AgentRun]:
    async with session_factory() as session:
        stmt = select(AgentRun).order_by(AgentRun.created_at, AgentRun.id)
        return list((await session.execute(stmt)).scalars().all())


async def run_status(session_factory, run_id: str) -> str | None:
    async with session_factory() as session:
        return (await session.get(AgentRun, uuid.UUID(run_id))).status


async def seed_attempt(session_factory, *, body: str, status: str) -> None:
    """造一条已存在的发布**尝试**（每条占一个自己的 Run + Draft）。

    `published_post` 的两个外键都非空，所以不能只插一行；这也顺带保证了
    "这条正文写过"会被 `list_recent_draft_contents` 看到——正是这道判据在
    影子模式下赖以工作的输入。
    """
    published = status == STATUS_PUBLISHED
    async with session_factory() as session:
        run = await create_agent_run(session, "占位题目")
        draft_id = await create_draft(
            session, str(run.id), body, version=1, prompt_version="test", model_name="fake"
        )
        session.add(
            PublishedPost(
                draft_id=uuid.UUID(draft_id),
                agent_run_id=run.id,
                body=body,
                status=status,
                x_post_id="1900000000000000009" if published else None,
                published_at=datetime.now(timezone.utc) if published else None,
            )
        )
        await session.commit()


async def tick(session_factory, config_path, *, x_client=None):
    """跑一轮 tick，返回 (结果, X 假客户端)。"""
    client = x_client or FakeXApiClient()
    x_service = XService(client, clock=lambda: FIXED_NOW)
    async with runtime_with_checkpointer(
        session_factory, x_service=x_service, llm_service=happy_llm()
    ) as runtime:
        result = await AutoPublishService(runtime, config_path=config_path).tick()
    return result, client


async def only_auto_review(session_factory):
    from app.models import AutoReview

    async with session_factory() as session:
        return (await session.execute(select(AutoReview))).scalars().one()


# ---------------------------------------------------------------------------
# 开关与配置
# ---------------------------------------------------------------------------


async def test_disabled_config_builds_no_run(session_factory, tmp_path):
    """刹车是"不动作"，不是"不发布"。

    关掉之后连 Run 都不该建：建 Run 要跑一整轮 Agent（LLM + X 检索），
    那是真金白银。只把发布那一步短路掉的话，关着开关的这一个月照样在烧钱。
    """
    path = write_config(tmp_path, enabled=False)

    result, client = await tick(session_factory, path)

    assert result.outcome == OUTCOME_DISABLED
    assert await all_runs(session_factory) == []
    assert client.created == []


async def test_broken_config_skips_the_tick_without_raising(session_factory, tmp_path):
    """配置写坏了 → 跳过这一轮，**不抛异常**。

    无人值守的东西最怕的是静默停摆。抛异常让进程以非 0 退出，计划任务下一轮
    照样拉起，于是失败只在事件日志里留下几百行一样的 traceback，没人在看。
    折进结构化结果至少每一轮都有一行说清楚发生了什么。
    """
    path = tmp_path / "automation.yaml"
    path.write_text("enabled: true\nmode: [这不是一个字符串\n", encoding="utf-8")

    result, _ = await tick(session_factory, str(path))

    assert result.outcome == OUTCOME_CONFIG_ERROR
    assert result.error
    assert await all_runs(session_factory) == []


async def test_unknown_mode_is_rejected_by_the_config(session_factory, tmp_path):
    """写错的 mode（例如大小写错了）必须响亮地失败。

    静默接受的话它会退化成"不是 live"，也就是**悄悄变成影子模式**——
    说好了要发，结果一直没发，而且没有任何地方说得出为什么。
    """
    path = write_config(tmp_path, mode="Shadow")

    result, _ = await tick(session_factory, path)

    assert result.outcome == OUTCOME_CONFIG_ERROR
    assert "mode" in (result.error or "")


def test_enabled_without_goals_is_a_config_error(tmp_path):
    """开着却没有目标 → 建 Run 那一步会抛，报的是"exactly one of topic/goal"，
    看起来像代码坏了。不如在加载时就拒绝。
    """
    from app.services.automation_config import load_automation_config

    path = write_config(tmp_path, goals=[])

    with pytest.raises(Exception, match="goals"):
        load_automation_config(path)


async def test_config_change_takes_effect_without_a_restart(session_factory, tmp_path):
    """改配置文件 = 热杀开关。这是这个功能唯一的刹车，必须每一轮都重读。"""
    path = write_config(tmp_path)

    first, _ = await tick(session_factory, path)
    assert first.outcome == OUTCOME_REVIEWED

    # 同一个路径，内容改掉，**不重启任何东西**
    write_config(tmp_path, enabled=False)
    second, _ = await tick(session_factory, path)

    assert second.outcome == OUTCOME_DISABLED
    assert len(await all_runs(session_factory)) == 1  # 没有第二条


# ---------------------------------------------------------------------------
# 影子模式：整个方案的核心断言
# ---------------------------------------------------------------------------


async def test_shadow_tick_records_a_verdict_and_publishes_nothing(session_factory, tmp_path):
    """**这是整个方案的核心断言。**

    一轮影子 tick 之后必须同时满足四件事：建了自动 Run、写了自动结论、
    Run 仍停在人工队列、**X 上一条都没发**。
    """
    path = write_config(tmp_path)

    result, client = await tick(session_factory, path)

    # 1) 判据跑完了，结论是通过
    assert result.outcome == OUTCOME_REVIEWED
    assert result.verdict == VERDICT_APPROVE
    assert result.approved is True
    # 2) 通过了，但**没有发布**——这就是"影子"的全部含义
    assert result.published is False
    assert client.created == []

    # 3) 自动 Run 建出来了，标着 scheduled
    runs = await all_runs(session_factory)
    assert len(runs) == 1
    assert runs[0].origin == ORIGIN_SCHEDULED
    assert runs[0].goal
    # 4) 但 Run 仍然停在人工队列里等人——不是自动通过、也不是自动拒绝
    assert runs[0].status == WAITING

    row = await only_auto_review(session_factory)
    assert row.verdict == VERDICT_APPROVE
    assert row.mode == MODE_SHADOW
    assert row.critic_score is not None
    # `acted` 的语义是"真的替人发了"，影子模式没发，所以必须是 False。
    # 若把"判据通过"当成 acted，这条记录日后看起来就像一条已发布的帖子。
    assert row.acted is False


async def test_topic_layer_does_not_compare_a_run_with_itself(session_factory, tmp_path):
    """回归：判据不能拿这条 Run 的题目跟它自己比。

    `finalize` 会把选定题目写进 `agent_run.topic`，而题目检测读的正是那一列。
    不排除当前 Run 的话，每一次自动运行都会因"最近已经写过同一个题目"被判
    不通过——影子模式显示 100% 误拦，而理由看起来完全合理。

    这里断言两件事：题目**确实**落库了（所以排除是必要的），
    以及重复检测的题目层**没有**命中。
    """
    path = write_config(tmp_path)

    result, _ = await tick(session_factory, path)

    runs = await all_runs(session_factory)
    assert runs[0].topic, "题目应当已写进库——正因如此才必须把这条 Run 排除掉"

    row = await only_auto_review(session_factory)
    topic_hits = [h for h in row.checks["repetition"]["hits"] if h["layer"] == "topic"]
    assert topic_hits == []
    assert result.approved is True


# ---------------------------------------------------------------------------
# 判据不通过 → **什么都不做**
# ---------------------------------------------------------------------------


async def test_low_critic_score_holds_the_run_for_a_human(session_factory, tmp_path):
    """判据不通过 ≠ 拒绝。Run 留在队列里，理由写进 auto_review。"""
    # 抬高门槛，让默认 8.0 分的候选过不去
    path = write_config(tmp_path, min_critic_score=9.9)

    result, client = await tick(session_factory, path)

    assert result.verdict == VERDICT_REJECT
    assert result.approved is False
    assert result.reasons
    assert client.created == []

    assert (await all_runs(session_factory))[0].status == WAITING  # 不是 rejected/failed

    row = await only_auto_review(session_factory)
    assert row.verdict == VERDICT_REJECT
    assert row.reasons
    assert row.checks["critic_score"] == {"passed": False, "value": 8.0, "threshold": 9.9}


async def test_hard_guardrail_reason_holds_the_run(session_factory, tmp_path):
    """guardrail 命中硬性问题 → 不放行，但同样不拒绝。

    这里选 `duplicate_content`（精确重复）作为注入手段：它是 Guardrail 在
    **Graph 内部**算出来的，所以这条测试同时验证了"tick 确实把 Graph 算出的
    原因码喂进了判据"，而不是另起一套自己的检查。
    """
    path = write_config(tmp_path, min_critic_score=1.0, max_publishes_per_day=10)

    # 三条候选都摆上：交付的是哪一条由 `pick_final_candidate` 决定，
    # 只摆一条会让这条测试依赖于那个选择。
    for index in (1, 2, 3):
        await seed_attempt(
            session_factory, body=candidate_content(index), status=STATUS_PUBLISHED
        )

    result, client = await tick(session_factory, path)

    assert result.approved is False
    assert client.created == []
    row = await only_auto_review(session_factory)
    assert row.checks["guardrail"]["blocking"] == ["duplicate_content"]
    assert (await all_runs(session_factory))[-1].status == WAITING


# ---------------------------------------------------------------------------
# 额度
# ---------------------------------------------------------------------------


async def test_run_cap_stops_before_paying_for_an_agent_round(session_factory, tmp_path):
    """额度检查必须在**建 Run 之前**。

    放在之后的话，每一轮超额的 tick 仍然会付掉一整轮 Agent 的钱
    （最多 12 步 LLM + 真实 X 检索）才发现该停。
    """
    path = write_config(tmp_path, max_auto_runs_per_day=1)
    async with session_factory() as session:
        await create_agent_run(session, None, goal="先占一个额度", origin=ORIGIN_SCHEDULED)

    result, client = await tick(session_factory, path)

    assert result.outcome == OUTCOME_RUN_CAP
    assert result.runs_today == 1
    assert len(await all_runs(session_factory)) == 1  # 没有第二条
    assert client.created == []


async def test_publish_cap_counts_attempts_not_just_successes(session_factory, tmp_path):
    """计入的是**尝试**次数：`publish_unknown` 的那一次可能已经发出去了。

    只数成功会让实际发文量悄悄越过上限。
    """
    path = write_config(tmp_path, min_critic_score=1.0, max_publishes_per_day=1)
    await seed_attempt(
        session_factory, body="不确定发出去没有的那条", status=STATUS_UNKNOWN
    )

    result, client = await tick(session_factory, path)

    assert result.publishes_today == 1
    assert result.approved is False
    assert client.created == []
    row = await only_auto_review(session_factory)
    assert row.checks["volume_cap"] == {"passed": False, "value": 1, "limit": 1}


# ---------------------------------------------------------------------------
# 残留 claim 的回收
# ---------------------------------------------------------------------------


async def test_stale_claim_is_returned_to_the_queue(session_factory, tmp_path):
    """上一次 tick 崩在 claim 之后、resume 之前的残留。

    那种 Run 状态已经是 approved，却没有 review 行：人审它得 409、
    发布它也 409，谁都处理不了。判据是"可发布状态 + 没有 review 行"——
    合法到达 approved 的 Run 一定有 review 行，因为两者出自同一段代码。
    """
    path = write_config(tmp_path, max_auto_runs_per_day=5)
    async with session_factory() as session:
        stale = await create_agent_run(session, None, goal="崩溃残留", origin=ORIGIN_SCHEDULED)
        # 摆成"claim 提交了、resume 没跑成"的样子
        stale.status = "approved"
        await session.commit()
        stale_id = str(stale.id)

    result, _ = await tick(session_factory, path)

    assert result.reaped == [stale_id]
    assert await run_status(session_factory, stale_id) == WAITING
    async with session_factory() as session:
        row = await session.get(AgentRun, uuid.UUID(stale_id))
    assert row.error and "interrupted" in row.error


async def test_reaper_never_touches_a_manual_run(session_factory, tmp_path):
    """**绝不**回收人发起的 Run。

    人也在 claim→resume 之间有一段窗口，而人和机器不共享那把锁。收人的 Run
    会把一个人"正在 resume 的那一次"重置回 waiting_for_review，
    他的 resume 随后照常写完 review 行，而 Run 状态已经变了——发布时撞上 409。
    """
    path = write_config(tmp_path, max_auto_runs_per_day=5)
    async with session_factory() as session:
        manual = await create_agent_run(session, None, goal="人的 Run")
        manual.status = "approved"
        await session.commit()
        manual_id = str(manual.id)

    result, _ = await tick(session_factory, path)

    assert result.reaped == []
    assert await run_status(session_factory, manual_id) == "approved"  # 原样不动


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------


async def test_a_second_concurrent_tick_gives_up(session_factory, tmp_path):
    """抢不到锁 → 这一轮直接放弃，**不排队等**。

    排队等下去只会让两个 tick 一前一后各建一个 Run，把当日额度加倍用掉，
    而计划任务下一轮马上又要来了。

    用 PostgreSQL 的 advisory lock 而不是 asyncio 锁，是因为 tick 有三个
    并发来源（计划任务 / 管理端点 / 本地 CLI）——三个进程，进程内的锁无效。
    """
    path = write_config(tmp_path)

    async with session_factory() as holder:
        assert await try_auto_publish_lock(holder) is True
        try:
            result, client = await tick(session_factory, path)
        finally:
            await release_auto_publish_lock(holder)

    assert result.outcome == OUTCOME_LOCKED
    assert await all_runs(session_factory) == []
    assert client.created == []


# ---------------------------------------------------------------------------
# 正式模式
# ---------------------------------------------------------------------------


async def test_live_mode_publishes_an_approved_draft(session_factory, tmp_path):
    from app.models import PublishedPost

    path = write_config(tmp_path, mode=MODE_LIVE)

    result, client = await tick(session_factory, path)

    assert result.published is True
    assert len(client.created) == 1

    assert (await all_runs(session_factory))[0].status == "published"

    row = await only_auto_review(session_factory)
    # auto 这条路也要留下审核记录，而且标着是自动做的决定——
    # 没有它，审核记录就答不出"这条帖子是机器人放行的还是人放行的"。
    assert row.acted is True

    async with session_factory() as session:
        posts = list((await session.execute(select(PublishedPost))).scalars().all())
    assert len(posts) == 1
    assert posts[0].body == client.created[0]


async def test_live_mode_still_publishes_nothing_when_rejected(session_factory, tmp_path):
    """不通过时两种模式的行为**完全一致**——差别只在通过那一侧。

    正是这条不对称让影子模式天然成立：影子期量出来的一致率，
    恰好就是正式期的行为。
    """
    path = write_config(tmp_path, mode=MODE_LIVE, min_critic_score=9.9)

    result, client = await tick(session_factory, path)

    assert result.published is False
    assert client.created == []
    assert (await all_runs(session_factory))[0].status == WAITING

    row = await only_auto_review(session_factory)
    assert row.acted is False


async def test_the_machines_own_approval_is_not_a_human_decision(
    session_factory, tmp_path
):
    """正式模式写下的那条 `review` 行是**机器的**决定，不能进"人的决定"那一列。

    正式模式下自动侧会重放 human_review 节点、留下一条 `decided_by='auto'` 的
    review 行（见 `auto_publish.py::_publish`）。那条行和它自己的 `auto_review`
    按 `draft_id` 天然配得上对，于是**每条自动放行的运行都会白送一个"一致"**。

    这个错的方向是单向的、而且最危险：机器只能在判通过时才动作，所以它写下的
    每条 review 行都必然是 approve，配上的 auto_review 也必然是 approve ——
    一致率被推向 1.0。而"一致率 ≥ 90%"正是转正式门槛的第 4 项。用这个数去决定
    能不能转正式，等于让机器**拿自己的判断给自己打分**。

    影子模式下没有这条行，所以影子期量出来的数是干净的 —— 污染恰好从"翻正"
    那一刻开始，也就是恰好从它开始算数的时刻开始。
    """
    from app.models import Review
    from app.models.review import DECIDED_BY_AUTO
    from app.repositories import shadow_agreement_stats
    from app.services.auto_review import summarize_agreement

    path = write_config(tmp_path, mode=MODE_LIVE)

    result, _client = await tick(session_factory, path)
    assert result.published is True, "前提不成立：这一轮没有真的走发布"

    async with session_factory() as session:
        reviews = list((await session.execute(select(Review))).scalars().all())
    # 前提：正式模式确实以**机器**的名义留下了审核记录。
    assert len(reviews) == 1
    assert reviews[0].decided_by == DECIDED_BY_AUTO
    assert reviews[0].status == "approve"

    async with session_factory() as session:
        summary = summarize_agreement(await shadow_agreement_stats(session))

    # 机器替自己点的这一次"批准"，不是人对机器的判断的表态。
    assert summary.decided_pairs == 0
    assert summary.agreed == 0
    assert summary.auto_approvals == 0
    assert summary.agreement_rate is None


async def test_a_manual_runs_auto_row_never_enters_the_shadow_matrix(
    session_factory, tmp_path
):
    """混淆矩阵只认 `origin='scheduled'`。

    正常路径**写不出**这种行——只有 tick 会写 `auto_review`，而 tick 只建
    scheduled 的 Run。所以这里手工造一条，钉住的是那道 `origin` 谓词本身：
    既然这个数的唯一用途是决定翻不翻正式，那么任何别的代码路径将来开始给
    人工 Run 记自动结论时，都不该把它的配对混进分母。

    与上面那条的区别：那条测的是"机器写的 review 不算人的决定"（`decided_by`），
    这条测的是"人工 Run 不算自动 Run"（`origin`）。两个谓词都得在。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, "人手的题")
        run_id = str(run.id)
        assert run.origin == "manual"
    async with session_factory() as session:
        draft_id = await create_draft(
            session, run_id, content="草稿正文", version=1, prompt_version="v1", model_name="fake"
        )
    async with session_factory() as session:
        await insert_auto_review(
            session,
            agent_run_id=run_id,
            draft_id=draft_id,
            mode=MODE_SHADOW,
            verdict=VERDICT_APPROVE,
            reasons=[],
            checks={},
            critic_score=8.0,
        )
    # 人也照常做了决定——这一对在**任何**其他口径下都是完整的一对。
    async with session_factory() as session:
        await create_review(
            session, draft_id=draft_id, action="approve", original_content="草稿正文"
        )

    async with session_factory() as session:
        summary = summarize_agreement(await shadow_agreement_stats(session))

    assert summary.decided_pairs == 0
    assert summary.auto_approvals == 0
    assert summary.agreement_rate is None


async def test_a_failed_publish_does_not_flip_the_auto_verdict(session_factory, tmp_path):
    """发布失败是**发布结果**，不是自动审核结论。

    判据说通过就是通过。X 那边挂了不该把 `verdict` 改成不通过，也不该把
    `acted` 改成 True——它压根没发出去。两条轴一旦合并，"机器当时怎么判的"
    这件事就再也答不出来，而门槛的每一条判据读的正是这个数。
    """
    from app.models import PublishedPost
    from app.services.x_api import XApiResponseError

    path = write_config(tmp_path, mode=MODE_LIVE)
    client = FakeXApiClient(post_error=XApiResponseError(402, "402 credits depleted"))

    result, _ = await tick(session_factory, path, x_client=client)

    assert result.published is False
    # 402 是"送到了、被拒了"，所以正文确实提交过一次——但**只有一次**。
    # 失败不重试是既有纪律（重试会撞上"状态说在等人、review 行却已写好"）。
    assert len(client.created) == 1

    row = await only_auto_review(session_factory)
    assert row.verdict == VERDICT_APPROVE, "判据的结论不该被发布结果改写"
    assert row.acted is False, "没发出去就不是 acted"

    async with session_factory() as session:
        posts = list((await session.execute(select(PublishedPost))).scalars().all())
    assert len(posts) == 1
    assert posts[0].x_post_id is None


async def test_an_unknown_publish_still_counts_as_having_acted(session_factory, tmp_path):
    """`acted` 的另一侧：结果**未知**时必须是 True。

    与上一条合起来，`acted` 的判据才完整：它问的不是"发布成功了吗"，
    而是"这条内容**有没有可能已经躺在 X 上了**"。`publish_unknown` 的整个
    意义就是"可能发出去了、要人去看"——把它算成没动作，等于在界面上否认
    那次可能已经公开的发布。

    这条上边界一旦丢失（比如为了让上一条通过而把条件收成"只有 published"），
    失守的方式是安静的：少一个 True，界面照样渲染。
    """
    from app.services.x_api import XApiTransportError

    path = write_config(tmp_path, mode=MODE_LIVE)
    client = FakeXApiClient(
        post_error=XApiTransportError("read timed out", may_have_reached_x=True)
    )

    result, _ = await tick(session_factory, path, x_client=client)

    assert result.published is False
    row = await only_auto_review(session_factory)
    assert row.verdict == VERDICT_APPROVE
    assert row.acted is True
