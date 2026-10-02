"""学习提案的接口测试（Phase 5D）：列表是现算的，库里的只有**人的裁决**。

这个文件盯的是一个设计的**边界**，而不是它的功能：提案本身是纯函数派生的
（不落库），落库的只有"谁在什么时候对哪条建议做了什么决定"。所以这里反复验
三件事：

1. **没人裁决过的提案不在库里**——列表页看得到它，数据库里找不到它；
2. **裁决是不可改写的**——同一条提案第二次裁决是 `409`，第一次的决定原样留着；
3. **裁决按下之后什么都不发生**——批准前后，同一份观察算出来的提案逐字相同，
   生产配置一个字段都没动。

另有一条只有真机数据才能验：当前真实数据（1 条帖 / 2 观察）下闸门是关的，
所以**连写端点都够不到**——`POST .../review` 对任何 id 都是 `404`。闸门挡住的是
"要不要提"，而它同时挡住了"要不要记"。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import text

from app.main import Runtime
from app.models import Draft, LearningProposalRecord, PostMetricsSnapshot, PublishedPost
from app.models.published_post import STATUS_PUBLISHED
from app.repositories import (
    create_agent_run,
    get_learning_proposal_decision,
    list_learning_proposal_decisions,
)
from app.services.admin import PROPOSALS_GATE_CLOSED_REASON
from app.style.schemas import StyleRecipe
from app.style.taxonomy import HookType, StructureType
from support import PERSONA, api_for

PUBLISHED_AT = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


class PerRunGraph:
    """按 `thread_id` 分派的 checkpoint 替身（同 `test_learning_api.py`）。"""

    def __init__(self, states: dict[str, dict]):
        self._states = states

    async def aget_state(self, config):
        thread_id = str((config or {}).get("configurable", {}).get("thread_id"))
        return SimpleNamespace(values=self._states.get(thread_id, {}))


@asynccontextmanager
async def proposals_api(session_factory, graph):
    runtime = Runtime(
        graph=graph,
        session_factory=session_factory,
        publish_service=None,  # 这组接口不该碰它；碰了会 AttributeError
        persona=PERSONA,
    )
    async with api_for(runtime) as api:
        yield api


def recipe(hook: HookType) -> StyleRecipe:
    return StyleRecipe(
        structure_type=StructureType.OPINION_FIRST,
        hook_type=hook,
        observed_target_length=120,
        hard_max_length=140,
    )


async def seed_post(
    session_factory,
    *,
    index: int,
    hook: HookType,
    likes: int | None = 10,
    replies: int | None = 2,
    reposts: int | None = 1,
    snapshots: int = 1,
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
            status=STATUS_PUBLISHED,
            x_post_id=f"21059980973020285{index:02d}",
            published_at=PUBLISHED_AT,
        )
        session.add(post)
        await session.flush()
        for i in range(snapshots):
            session.add(
                PostMetricsSnapshot(
                    published_post_id=post.id,
                    x_post_id=post.x_post_id,
                    observed_at=PUBLISHED_AT + timedelta(hours=48.0 + i),
                    hours_since_publish=48.0 + i,
                    likes=likes,
                    replies=replies,
                    reposts=reposts,
                    quotes=0,
                )
            )
        await session.commit()
        return str(run.id)


async def seed_real_data_shape(session_factory) -> PerRunGraph:
    """真机当前的样子：**1 条帖 / 2 次观察**。"""
    run_id = await seed_post(
        session_factory, index=0, hook=HookType.QUESTION, snapshots=2
    )
    return PerRunGraph({run_id: {"selected_topic": None, "style_recipe": recipe(HookType.QUESTION)}})


async def seed_balanced(session_factory) -> PerRunGraph:
    """5 条 question 各 20 赞 + 5 条 statement 各 8 赞，三指标同向。

    只有三个活动计数**全部**同向时这一层才提案（见
    `tests/test_learning_proposal.py::test_a_direction_must_be_unanimous_across_the_metrics`），
    所以这里的低分那一组三个计数都要更低。
    """
    states: dict[str, dict] = {}
    for index, hook in enumerate([HookType.QUESTION] * 5 + [HookType.STATEMENT] * 5):
        high = hook is HookType.QUESTION
        run_id = await seed_post(
            session_factory,
            index=index,
            hook=hook,
            likes=20 if high else 8,
            replies=12 if high else 4,
            reposts=6 if high else 1,
        )
        states[run_id] = {"selected_topic": None, "style_recipe": recipe(hook)}
    return PerRunGraph(states)


async def stored_decisions(session_factory) -> list[LearningProposalRecord]:
    async with session_factory() as session:
        return list(await list_learning_proposal_decisions(session))


# ---------------------------------------------------------------------------
# 一、真机数据：闸门关着，零条，而且连写端点都够不到
# ---------------------------------------------------------------------------


async def test_real_data_shape_offers_no_proposal_and_says_why(session_factory):
    graph = await seed_real_data_shape(session_factory)
    async with proposals_api(session_factory, graph) as api:
        body = (await api.get("/api/admin/learning/proposals")).json()

    assert body["available"] is True, "有记录就是「有数据」，只是数据不够"
    assert body["learning_ready"] is False
    assert body["proposal_count"] == 0
    assert body["proposals"] == []
    # 断言用的是**哪一条**原因，而不是它的措辞：闸门关着、没有数据、数据够用
    # 三条原因里必须挑中"闸门关着"那条——挑了别的（或空串）都在说别的事。
    assert body["reason"] == PROPOSALS_GATE_CLOSED_REASON


async def test_the_overview_count_agrees_with_the_proposal_list(session_factory):
    """两个端点说的是同一件事：`/learning` 的 `proposal_count` 是现算的。"""
    graph = await seed_real_data_shape(session_factory)
    async with proposals_api(session_factory, graph) as api:
        overview = (await api.get("/api/admin/learning")).json()
        proposals = (await api.get("/api/admin/learning/proposals")).json()

    assert overview["proposal_count"] == proposals["proposal_count"] == 0


async def test_the_gate_also_blocks_the_write_path(session_factory):
    """闸门关着时**没有任何 id 可以裁决**——包括一个看起来很合理的 id。"""
    graph = await seed_real_data_shape(session_factory)
    async with proposals_api(session_factory, graph) as api:
        response = await api.post(
            "/api/admin/learning/proposals/proposal:hook_type@24-72h/review",
            json={"action": "approve"},
        )
    assert response.status_code == 404
    assert await stored_decisions(session_factory) == []


# ---------------------------------------------------------------------------
# 二、数据够了：提案现算出来，但一条都不落库
# ---------------------------------------------------------------------------


async def test_balanced_data_offers_exactly_one_reviewable_proposal(session_factory):
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        body = (await api.get("/api/admin/learning/proposals")).json()

    assert body["learning_ready"] is True
    assert body["proposal_count"] == 1
    assert body["reviewable_count"] == 1
    assert body["decided_count"] == 0

    row = body["proposals"][0]
    assert row["target"] == "style_recipe.hook_type"
    assert row["target_label"]
    assert row["evidence_ids"], "提案必须带证据 id"
    assert row["sample_count"] == 10, "样本量是**帖子并集**，不是三条指标相加的 30"
    assert row["status"] == "proposed"
    assert row["reviewable"] is True
    assert row["validation"]["valid"] is True
    assert row["validation"]["reasons"] == []


async def test_a_proposal_carries_its_boundaries_and_no_confidence(session_factory):
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        row = (await api.get("/api/admin/learning/proposals")).json()["proposals"][0]

    assert row["confidence"] is None, "本仓库没有校准过的概率模型，不该有置信度"
    assert row["limitations"], "提案必须带上它的适用边界"
    assert row["automatic_application"] is False
    assert "score" not in row, "这一层没有 proposal_score（工作单第十七节）"
    assert "priority" not in row


async def test_reading_the_list_stores_nothing(session_factory):
    """**本设计的关键**：没人裁决过的提案不进数据库。"""
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        await api.get("/api/admin/learning/proposals")
        await api.get("/api/admin/learning/proposals")
        await api.get("/api/admin/learning")

    assert await stored_decisions(session_factory) == [], (
        "只读接口把提案写进了库——它应当只在被裁决时落一条裁决快照"
    )


# ---------------------------------------------------------------------------
# 三、裁决：记下来，然后什么都不发生
# ---------------------------------------------------------------------------


async def test_approving_records_a_decision_and_nothing_else(session_factory):
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        before = (await api.get("/api/admin/learning/proposals")).json()
        proposal_id = before["proposals"][0]["id"]

        response = await api.post(
            f"/api/admin/learning/proposals/{proposal_id}/review",
            json={"action": "approve", "note": "下个阶段可以试"},
        )
        assert response.status_code == 200, response.text
        decided = response.json()

        # 批准**没有**改变这条提案本身：目标、当前值、建议值、依据全都原样。
        original = before["proposals"][0]
        for field in ("target", "current_value", "proposed_value", "evidence_ids", "sample_count"):
            assert decided[field] == original[field], field
        assert decided["status"] == "approved"
        assert decided["reviewable"] is False
        assert decided["review_note"] == "下个阶段可以试"
        assert decided["automatic_application"] is False

        after = (await api.get("/api/admin/learning/proposals")).json()
        # 观察没有变，所以提案还是那一条；变的只是"它被裁决过了"。
        assert after["proposal_count"] == 1
        assert after["reviewable_count"] == 0
        assert after["decided_count"] == 1
        assert after["proposals"][0]["status"] == "approved"

        overview = (await api.get("/api/admin/learning")).json()

    assert overview["readiness"]["learning_ready"] is True
    assert overview["proposal_count"] == 1, "裁决不改观察，提案数不该变"

    stored = await stored_decisions(session_factory)
    assert len(stored) == 1
    assert stored[0].proposal_key == proposal_id
    assert stored[0].status == "approved"
    assert stored[0].reviewed_at is not None
    assert stored[0].note == "下个阶段可以试"


async def test_rejecting_requires_a_reason_and_records_it(session_factory):
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        proposal_id = (await api.get("/api/admin/learning/proposals")).json()["proposals"][0]["id"]

        # 没有理由的否决 → 422，且**什么都没写**。
        empty = await api.post(
            f"/api/admin/learning/proposals/{proposal_id}/review",
            json={"action": "reject"},
        )
        assert empty.status_code == 422
        blank = await api.post(
            f"/api/admin/learning/proposals/{proposal_id}/review",
            json={"action": "reject", "note": "   "},
        )
        assert blank.status_code == 422
        assert await stored_decisions(session_factory) == []

        ok = await api.post(
            f"/api/admin/learning/proposals/{proposal_id}/review",
            json={"action": "reject", "note": "这批数据只看得出方向，看不出稳不稳"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "rejected"

    stored = await stored_decisions(session_factory)
    assert len(stored) == 1
    assert stored[0].status == "rejected"


async def test_a_decision_is_append_only_and_cannot_be_overwritten(session_factory):
    """裁决是审计记录：第二次裁决 `409`，第一次的决定**原样留着**。"""
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        proposal_id = (await api.get("/api/admin/learning/proposals")).json()["proposals"][0]["id"]
        first = await api.post(
            f"/api/admin/learning/proposals/{proposal_id}/review",
            json={"action": "approve", "note": "先批"},
        )
        assert first.status_code == 200

        second = await api.post(
            f"/api/admin/learning/proposals/{proposal_id}/review",
            json={"action": "reject", "note": "改主意了"},
        )
        assert second.status_code == 409

    stored = await stored_decisions(session_factory)
    assert len(stored) == 1, "第二次裁决写了第二行——它本该被唯一约束挡回去"
    assert stored[0].status == "approved"
    assert stored[0].note == "先批"


async def test_reviewing_an_unknown_proposal_is_not_found(session_factory):
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        response = await api.post(
            "/api/admin/learning/proposals/proposal:viewpoint.stance@24-72h/review",
            json={"action": "approve"},
        )
    assert response.status_code == 404
    assert await stored_decisions(session_factory) == []


async def test_a_decision_survives_the_proposal_no_longer_being_produced(session_factory):
    """当时裁决过、现在算不出来的提案**仍然列出来**，并说明为什么。

    反着做更糟：一条批准过的历史裁决若从界面上消失，读的人会以为从来没人
    批准过什么。
    """
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        proposal_id = (await api.get("/api/admin/learning/proposals")).json()["proposals"][0]["id"]
        await api.post(
            f"/api/admin/learning/proposals/{proposal_id}/review",
            json={"action": "approve", "note": "记下来"},
        )
        # 换一个**没有读数**的窗口：现在算不出任何提案。
        body = (await api.get("/api/admin/learning/proposals?window=1-6h")).json()

    assert body["proposal_count"] == 0
    assert body["decided_count"] == 1
    assert len(body["proposals"]) == 1
    stale = body["proposals"][0]
    assert stale["id"] == proposal_id
    assert stale["status"] == "approved"
    assert stale["stale_reason"], "一条算不出来的历史裁决必须说明它为什么在这儿"
    assert stale["reviewable"] is False


# ---------------------------------------------------------------------------
# 四、结构：这张表存得下什么、存不下什么
# ---------------------------------------------------------------------------

#: `learning_proposal` 的全部列。**封闭清单**：多一列都要在这里被看见，
#: 因为工作单第三十四节禁止保存 full prompt / LLM secret / raw completion，
#: 而"禁止"只有在这一层可判时才不是一句口号。
_EXPECTED_COLUMNS = {
    "id",
    "proposal_key",
    "target",
    "current_value",
    "proposed_value",
    "evidence_ids",
    "rationale",
    "limitations",
    "sample_count",
    "observation_window",
    "current_recipe_version",
    "proposed_recipe_version",
    "status",
    "created_at",
    "reviewed_at",
    "note",
}


def test_the_table_stores_a_decision_and_nothing_that_could_be_applied():
    columns = set(LearningProposalRecord.__table__.columns.keys())
    assert columns == _EXPECTED_COLUMNS
    # 没有"已应用"这种列：本阶段不实现应用（工作单第三十五节）。
    for forbidden in ("applied_at", "applied_by", "applied", "prompt", "completion", "score"):
        assert forbidden not in columns, f"{forbidden!r} 不该出现在这张表里"


def test_the_only_statuses_ever_stored_are_the_two_human_decisions():
    """表里只存**人做过的**决定：没有 `proposed`（那还没决定），也没有 `expired`。

    `expired` 是**读的时候**算出来的（同一个 `approved` 在不同的现场可能已经过期），
    不是一个写进去的状态。
    """
    from app.services.admin import PROPOSAL_ACTION_STATUS

    assert set(PROPOSAL_ACTION_STATUS.values()) == {"approved", "rejected"}
    assert PROPOSAL_ACTION_STATUS == {"approve": "approved", "reject": "rejected"}


async def test_the_stored_row_is_self_describing(session_factory):
    """一条裁决几个月后要能自己解释自己——那时它的提案可能已经不再被生成。"""
    graph = await seed_balanced(session_factory)
    async with proposals_api(session_factory, graph) as api:
        proposal_id = (await api.get("/api/admin/learning/proposals")).json()["proposals"][0]["id"]
        await api.post(
            f"/api/admin/learning/proposals/{proposal_id}/review",
            json={"action": "approve"},
        )

    async with session_factory() as session:
        record = await get_learning_proposal_decision(session, proposal_id)

    assert record is not None
    assert record.rationale, "裁决快照必须自己带着理由"
    assert record.limitations, "裁决快照必须自己带着边界"
    assert record.evidence_ids, "裁决快照必须自己带着证据引用"
    assert record.sample_count == 10
    assert record.observation_window == "24-72h"


def test_reading_the_table_never_updates_it():
    """追加写，没有 UPDATE 路径：`repositories` 里针对这张表只有读与插。"""
    from app import repositories

    source = open(repositories.__file__, encoding="utf-8").read()
    assert "update(LearningProposalRecord)" not in source
    assert "learning_proposal" in source


async def test_the_decision_table_has_no_foreign_key_to_anything(session_factory):
    """裁决引用的是证据 id 与配方版本号，不是行 id——所以它谁都不挂。

    这条不是洁癖：`tests/support.py` 靠"它没有外键"来解释为什么
    `TRUNCATE ... CASCADE` 够不到它、必须显式清。外键一旦加上，
    那句解释就成了错的，而残留行带来的失败会看起来像提案生成出了问题。
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = "
                    "'learning_proposal'::regclass AND contype = 'f'"
                )
            )
        ).all()
    assert rows == []
