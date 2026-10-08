"""自动发布的 HTTP 面：tick 端点与状态端点。

这两个端点承担的事情很不一样，所以测试的重点也不一样：

- `POST /auto-publish/tick` 是**控制台唯一会真的发帖**的端点。它默认不存在
  （没配令牌就 403），所以这里最重要的是"拒绝"那一侧，以及"拒绝了就什么都没发生"。
- `GET /auto-publish/status` 是只读的，但它的输出是**转正式的唯一依据**。
  这里最要紧的是 `None ≠ 0`：还没跑过影子时，"没有数据"不能用一串 0 冒充。

`outcome` 不是 HTTP 状态是刻意的（见端点说明），所以下面大量断言 200 +
`outcome == "disabled"` 这种组合——那正是"调度器要的是跑过了，不是成功了"。
"""

import uuid

import yaml
from sqlalchemy import select

from app.config import settings
from app.models import AgentRun, AutoReview
from app.repositories import create_review
from app.services.auto_publish import OUTCOME_DISABLED, OUTCOME_REVIEWED
from app.services.x_service import XService
from fakes import FakeXApiClient
from support import FIXED_NOW, api_for, runtime_with_checkpointer
from test_agent_loop import happy_llm

TICK = "/api/admin/auto-publish/tick"
STATUS = "/api/admin/auto-publish/status"


def write_config(tmp_path, **overrides) -> str:
    """写一份 automation.yaml。默认是"开着 + 影子模式"。"""
    data = {
        "enabled": True,
        "mode": "shadow",
        "goals": ["找一个值得讨论的 AI 话题，写一条有明确观点的帖子"],
        "max_auto_runs_per_day": 3,
        "max_publishes_per_day": 1,
        "min_critic_score": 7.0,
        "tick_timeout_seconds": 120,
    }
    data.update(overrides)
    path = tmp_path / "automation.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


def runtime_factory(session_factory, client):
    return runtime_with_checkpointer(
        session_factory,
        x_service=XService(client, clock=lambda: FIXED_NOW),
        llm_service=happy_llm(),
    )


async def all_runs(session_factory) -> list[AgentRun]:
    async with session_factory() as session:
        stmt = select(AgentRun).order_by(AgentRun.created_at, AgentRun.id)
        return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# POST /auto-publish/tick —— 拒绝那一侧
# ---------------------------------------------------------------------------


async def test_tick_refuses_everything_when_no_token_is_configured(
    session_factory, tmp_path, monkeypatch
):
    """**没配令牌 = 端点不存在。**

    这是刻意的失败方向。本仓库没有任何鉴权设施，与其为这一个端点发明一套，
    不如让它默认打不开——不配令牌就用 CLI，那本来就是主机制。

    关键的第二个断言是"什么都没发生"：403 必须是**在动作之前**返回的，
    否则一个未授权的请求仍然会建 Run、烧掉一整轮 Agent 的钱。
    """
    monkeypatch.setattr(settings, "auto_publish_tick_token", "")
    monkeypatch.setattr(settings, "automation_config_path", write_config(tmp_path))
    client = FakeXApiClient()

    async with runtime_factory(session_factory, client) as runtime:
        async with api_for(runtime) as api:
            response = await api.post(TICK, headers={"X-Auto-Publish-Token": "anything"})

    assert response.status_code == 403
    assert "AUTO_PUBLISH_TICK_TOKEN" in response.json()["detail"]
    assert await all_runs(session_factory) == []
    assert client.created == []


async def test_tick_refuses_an_empty_token_header(session_factory, tmp_path, monkeypatch):
    """`X-Auto-Publish-Token: ` 这种空头也要 403。

    空字符串是最容易被"顺着写过去"的输入：`if token != configured` 配上
    `configured=""` 之外的任何值都是 403，但若配置恰好是空串——那正是
    "没配令牌"的情形，由上面那条测试守着。这里钉的是"配了令牌之后，
    空头仍然不算数"。
    """
    monkeypatch.setattr(settings, "auto_publish_tick_token", "s3cret")
    monkeypatch.setattr(settings, "automation_config_path", write_config(tmp_path))

    async with runtime_factory(session_factory, FakeXApiClient()) as runtime:
        async with api_for(runtime) as api:
            response = await api.post(TICK, headers={"X-Auto-Publish-Token": ""})

    assert response.status_code == 403
    assert await all_runs(session_factory) == []


async def test_tick_refuses_a_wrong_token(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_publish_tick_token", "s3cret")
    monkeypatch.setattr(settings, "automation_config_path", write_config(tmp_path))

    async with runtime_factory(session_factory, FakeXApiClient()) as runtime:
        async with api_for(runtime) as api:
            response = await api.post(TICK, headers={"X-Auto-Publish-Token": "s3cre"})

    assert response.status_code == 403
    assert response.json()["detail"] == "invalid auto publish token"
    assert await all_runs(session_factory) == []


# ---------------------------------------------------------------------------
# POST /auto-publish/tick —— 放行那一侧
# ---------------------------------------------------------------------------


async def test_tick_runs_with_the_right_token(session_factory, tmp_path, monkeypatch):
    """令牌对了就跑一轮，并且**如实报告跑了什么**。

    这里顺带钉住"HTTP 200 ≠ 发了帖"：影子模式下这一轮 200 且
    `verdict=approve`，而 X 上一条都没有。
    """
    monkeypatch.setattr(settings, "auto_publish_tick_token", "s3cret")
    monkeypatch.setattr(settings, "automation_config_path", write_config(tmp_path))
    client = FakeXApiClient()

    async with runtime_factory(session_factory, client) as runtime:
        async with api_for(runtime) as api:
            response = await api.post(TICK, headers={"X-Auto-Publish-Token": "s3cret"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == OUTCOME_REVIEWED
    assert body["verdict"] == "approve"
    assert body["published"] is False
    assert body["run_id"]
    assert client.created == []
    assert len(await all_runs(session_factory)) == 1


async def test_tick_reports_a_disabled_switch_as_200_not_an_error(
    session_factory, tmp_path, monkeypatch
):
    """**关着不是错误。** 调度器要的是"这一轮被处理过了"。

    把 `disabled` 变成 4xx/5xx 会让计划任务的退出码变成失败——而那正是
    用户按暂停时会发生的事，于是一个正常的暂停看起来像系统坏了。
    """
    monkeypatch.setattr(settings, "auto_publish_tick_token", "s3cret")
    monkeypatch.setattr(settings, "automation_config_path", write_config(tmp_path, enabled=False))

    async with runtime_factory(session_factory, FakeXApiClient()) as runtime:
        async with api_for(runtime) as api:
            response = await api.post(TICK, headers={"X-Auto-Publish-Token": "s3cret"})

    assert response.status_code == 200
    assert response.json()["outcome"] == OUTCOME_DISABLED
    assert await all_runs(session_factory) == []


# ---------------------------------------------------------------------------
# GET /auto-publish/status —— 没有数据时
# ---------------------------------------------------------------------------


async def test_status_says_no_data_rather_than_zero(session_factory, tmp_path, monkeypatch):
    """**整个方案里最容易写错的一个数。**

    还没跑过影子时，`agreement_rate` 必须是 `None`。写成 `0.0` 会让界面上
    出现一个"一致率 0%"的数字，而它的真实含义是"还没有数据"——两者都通往
    "先别转正式"，但一个是对的，一个会让人去查一个根本不存在的问题。
    """
    monkeypatch.setattr(settings, "automation_config_path", write_config(tmp_path))

    async with runtime_factory(session_factory, FakeXApiClient()) as runtime:
        async with api_for(runtime) as api:
            response = await api.get(STATUS)

    assert response.status_code == 200
    body = response.json()

    agreement = body["agreement"]
    assert agreement["decided_pairs"] == 0
    assert agreement["agreement_rate"] is None
    assert agreement["soft_mismatch_rate"] is None

    # 门槛必须是不通过，而且**每条判据都说得出为什么**——
    # 一串 met=false 而没有 detail 的判据在界面上等于没有。
    assert body["gate"]["ready"] is False
    assert body["gate"]["criteria"]
    assert all(c["detail"] for c in body["gate"]["criteria"])

    # 机器判断不了的那几步每次都下发（同 evaluate_go_live_gate 的注释）。
    assert body["gate"]["manual_steps"]

    # 当前配置随数一起下发：门槛结论只有在"当时阈值是什么"的前提下才有意义。
    assert body["enabled"] is True
    assert body["mode"] == "shadow"
    assert body["min_critic_score"] == 7.0


async def test_status_still_reports_data_when_the_config_is_broken(
    session_factory, tmp_path, monkeypatch
):
    """配置坏了，状态端点仍然要能回答。

    它算的是**库里已有的数据**，而"要不要改配置"恰恰要靠这些数据来判断——
    因为配置读不了就把一致性统计也一起藏起来，正好在最需要它的时候把它拿走。
    """
    broken = tmp_path / "automation.yaml"
    broken.write_text("enabled: true\nmode: halo\n", encoding="utf-8")
    monkeypatch.setattr(settings, "automation_config_path", str(broken))

    async with runtime_factory(session_factory, FakeXApiClient()) as runtime:
        async with api_for(runtime) as api:
            response = await api.get(STATUS)

    assert response.status_code == 200
    body = response.json()
    assert body["config_error"]
    assert "mode" in body["config_error"]
    # 安全默认：读不懂的配置 = 关着 + 影子。
    assert body["enabled"] is False
    assert body["mode"] == "shadow"
    assert body["agreement"]["decided_pairs"] == 0


# ---------------------------------------------------------------------------
# GET /auto-publish/status —— 有数据时
# ---------------------------------------------------------------------------


async def test_status_counts_a_real_auto_human_pair(session_factory, tmp_path, monkeypatch):
    """一轮影子 tick + 人照常批准 → 一个**一致**的配对。

    这条测的是整条度量链真的接通了：自动结论写进 `auto_review`、人的决定写进
    `review`、两者按 `draft_id` 配对上、并且被算成"一致"。任何一环断了，
    转正式的门槛就永远停在 0 —— 而那看起来只是"数据还不够"。
    """
    from app.services.auto_publish import AutoPublishService

    config_path = write_config(tmp_path)
    monkeypatch.setattr(settings, "automation_config_path", config_path)

    # 用**服务**跑一轮，而不是手写 auto_review 行：手写就绕过了"编排器是否
    # 真的写了这条记录"，而那正是被测的东西。
    async with runtime_factory(session_factory, FakeXApiClient()) as runtime:
        await AutoPublishService(runtime, config_path=config_path).tick()

        async with session_factory() as session:
            auto = (await session.execute(select(AutoReview))).scalars().one()
            run = (await session.execute(select(AgentRun))).scalars().one()
        # 人照常审这条 Run（影子模式下它本来就停在队列里等人）
        async with session_factory() as session:
            await create_review(
                session,
                draft_id=str(auto.draft_id),
                action="approve",
                original_content="人看到的原稿",
            )

        async with api_for(runtime) as api:
            response = await api.get(STATUS)

    assert response.status_code == 200
    agreement = response.json()["agreement"]
    assert agreement["decided_pairs"] == 1
    assert agreement["agreed"] == 1
    assert agreement["false_approve"] == 0
    assert agreement["agreement_rate"] == 1.0
    # 样本远不够，所以门槛仍然不通过——**有数据**与**够数据**是两件事。
    assert response.json()["gate"]["ready"] is False
    assert run.origin == "scheduled"


async def test_status_notices_an_impossible_auto_approval(
    session_factory, tmp_path, monkeypatch
):
    """门禁判据**不是恒真的**。

    "自动放行 + 内容门禁非空"按 `evaluate_auto_publish` 的构造不可能出现，
    所以这条判据在正常数据上恒过——而一条恒过的判据等于没有判据。这里手工
    写出一条矛盾记录（只能这样造，因为正常路径写不出来），验证它确实会被发现。

    这条测试守的是那个 JSONB 路径查询本身：路径写错（比如少一层）时
    `as_boolean()` 会永远取不到值，判据就会**静默地恒过**，
    而它恰好是转正式门槛里的硬阻断项之一。
    """
    from app.repositories import insert_auto_review

    monkeypatch.setattr(settings, "automation_config_path", write_config(tmp_path))

    async with session_factory() as session:
        run = AgentRun(topic="占位", status="waiting_for_review", origin="scheduled")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        from app.repositories import create_draft

        draft_id = await create_draft(
            session, str(run.id), "正文", version=1, prompt_version="t", model_name="f"
        )
    async with session_factory() as session:
        await insert_auto_review(
            session,
            agent_run_id=str(run.id),
            draft_id=draft_id,
            mode="shadow",
            verdict="approve",
            reasons=["不该有理由"],
            # 与 verdict 矛盾的 checks —— 正是"记录与判据不一致"
            checks={
                "content_gate": {"passed": False, "violations": ["internal_ref_leak"]},
                "guardrail": {"passed": True, "blocking": [], "advisory": []},
                "critic": {"passed": True, "candidate_id": "c1", "decision": "accept"},
                "critic_score": {"passed": True, "value": 8.0, "threshold": 7.0},
                "repetition": {"passed": True, "hits": [], "lexical_containment": None},
                "volume_cap": {"passed": True, "value": 0, "limit": 1},
            },
            critic_score=8.0,
        )

    async with runtime_factory(session_factory, FakeXApiClient()) as runtime:
        async with api_for(runtime) as api:
            response = await api.get(STATUS)

    criterion = next(
        c for c in response.json()["gate"]["criteria"] if c["key"] == "content_gate"
    )
    assert criterion["met"] is False
    assert "不应当发生" in criterion["detail"]
    # 其它判据不受影响——否则这条测试就说不清是哪一项把门槛挡下的。
    others = {c["key"]: c["met"] for c in response.json()["gate"]["criteria"]}
    assert others["sample"] is False  # 样本还不够，但这是另一条理由


async def test_status_is_read_only(session_factory, tmp_path, monkeypatch):
    """状态端点**不改任何东西**：不跑 tick、不改配置、不发布。

    它旁边就是会真的发帖的 tick 端点，而这个端点的存在理由恰好是"翻配置之前
    先看看数"——如果看数本身有副作用，那这个理由就不成立了。
    """
    monkeypatch.setattr(settings, "automation_config_path", write_config(tmp_path))
    client = FakeXApiClient()

    async with runtime_factory(session_factory, client) as runtime:
        async with api_for(runtime) as api:
            first = await api.get(STATUS)
            second = await api.get(STATUS)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()  # 幂等到连响应体都一样
    assert await all_runs(session_factory) == []
    assert client.created == []
