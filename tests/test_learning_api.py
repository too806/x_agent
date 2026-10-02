"""`GET /api/admin/learning` —— 只读的学习准备度一览（Phase 5C）。

这个接口与别的只读接口有一处不同：它是控制台里**唯一**按条读 checkpoint 的
聚合端点（分组维度只存在于 checkpoint）。所以这里除了形状与闸门，还要盯住一件事：

    **它不许写任何东西，也不许改任何东西。**

真实数据现在是 1 条帖 / 2 条观察。那种数据下正确的输出是
`learning_ready=false` + `reason="insufficient_sample"`——一个**结论**，
不是一个看起来中性的空报告。这个文件里有一半的测试在钉这一点。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import text

from app.main import Runtime
from app.models import Draft, PostMetricsSnapshot, PublishedPost
from app.models.published_post import STATUS_PUBLISHED
from app.repositories import create_agent_run
from app.style.schemas import StyleRecipe
from app.style.taxonomy import HookType, StructureType
from support import PERSONA, api_for

PUBLISHED_AT = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


class PerRunGraph:
    """按 `thread_id` 分派的 checkpoint 替身。

    不能用 `tests/fakes.py::StubGraph`：它对**每个** thread_id 返回同一份 state，
    而本接口恰恰要读**每条帖子各自的**决策字段。用固定 state 的话，
    "两个 cohort 取值不同"这件事在测试里根本无法构造。
    """

    def __init__(self, states: dict[str, dict]):
        self._states = states
        self.reads: list[str] = []

    async def aget_state(self, config):
        thread_id = str((config or {}).get("configurable", {}).get("thread_id"))
        self.reads.append(thread_id)
        return SimpleNamespace(values=self._states.get(thread_id, {}))


@asynccontextmanager
async def learning_api(session_factory, graph):
    runtime = Runtime(
        graph=graph,
        session_factory=session_factory,
        publish_service=None,  # 只读接口不该碰它；碰了会 AttributeError
        persona=PERSONA,
    )
    async with api_for(runtime) as api:
        yield api


async def seed_post(
    session_factory,
    *,
    index: int,
    hook: HookType,
    likes: int | None = 10,
    replies: int | None = 2,
    reposts: int | None = 1,
    quotes: int | None = 0,
    snapshots: int = 1,
    hours: float = 48.0,
) -> str:
    """一条"已发布 + N 次观察"的帖子，返回它的 Run id（= checkpoint 的 thread_id）。

    刻意用 Run id 而不是帖子 id：本接口读的是 **checkpoint**，而 checkpoint
    是按 Run 存的。两个 id 不是一回事，混用会让测试在读到空 state 时**静默**
    退化成"没有分组"，于是一条本该失败的断言反而通过。
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
                    observed_at=PUBLISHED_AT + timedelta(hours=hours + i),
                    hours_since_publish=hours + i,
                    likes=likes,
                    replies=replies,
                    reposts=reposts,
                    quotes=quotes,
                )
            )
        await session.commit()
        return str(run.id)


def recipe(hook: HookType) -> StyleRecipe:
    return StyleRecipe(
        structure_type=StructureType.OPINION_FIRST,
        hook_type=hook,
        observed_target_length=120,
        hard_max_length=140,
    )


async def seed_balanced(session_factory, *, likes_for: str | None = None) -> PerRunGraph:
    """5 条 question + 5 条 statement，全部落在同一个观察窗口。

    两组的写法参数只有 `hook_type` 不同，于是 hook_type 是这份数据里唯一能
    构成比较的维度。`likes_for` 指定哪一组**读不到**点赞（`None`），
    用来验"缺失不补 0"这条最容易写坏的支路。
    """
    states: dict[str, dict] = {}
    for index, hook in enumerate([HookType.QUESTION] * 5 + [HookType.STATEMENT] * 5):
        likes = None if likes_for == hook.value else 10 + index
        run_id = await seed_post(session_factory, index=index, hook=hook, likes=likes)
        states[run_id] = {"selected_topic": None, "style_recipe": recipe(hook)}
    return PerRunGraph(states)


# ---------------------------------------------------------------------------
# 真机当前数据的样子：一条帖子、两次观察
# ---------------------------------------------------------------------------


async def test_learning_without_posts_reports_a_reason_not_zeros(session_factory):
    """一条已发布帖子都没有：`available=false` + 原因，不返回一串 0。"""
    graph = PerRunGraph({})
    async with learning_api(session_factory, graph) as api:
        data = (await api.get("/api/admin/learning")).json()

    assert data["available"] is False
    assert data["reason"]
    assert data["post_count"] == 0
    assert data["sample_count"] == 0
    assert data["cohort_count"] == 0
    assert data["valid_comparison_count"] == 0
    assert data["cohorts"] == []
    assert data["comparisons"] == []
    # 没有数据时不能说"学习准备度是 0 分"之类的话——只有一个明确的原因。
    assert data["readiness"]["learning_ready"] is False
    assert data["readiness"]["reason"] == "insufficient_sample"
    assert data["readiness"]["automatic_modification"] is False


async def test_one_post_with_two_snapshots_is_still_one_sample(session_factory):
    """真实库的形态：**1 条帖 / 2 次观察**。分析单位是帖子，所以这是 n=1。

    把两条快照当成两个样本，是在小样本上造"显著差异"最快的一条路。
    这条测试就是钉死"没有发生这件事"。
    """
    run_id = await seed_post(
        session_factory, index=1, hook=HookType.QUESTION, likes=0, snapshots=2
    )
    graph = PerRunGraph({run_id: {"selected_topic": None, "style_recipe": recipe(HookType.QUESTION)}})

    async with learning_api(session_factory, graph) as api:
        data = (await api.get("/api/admin/learning")).json()

    assert data["available"] is True
    assert data["post_count"] == 1
    assert data["sample_count"] == 1
    assert data["readiness"]["learning_ready"] is False
    assert data["readiness"]["reason"] == "insufficient_sample"
    # 两次观察都落在同一个窗口（48h / 49h → 24-72h），所以这是"窗口内取了最新那条"，
    # 不是"两个窗口各一条"。
    assert data["observation_window"] == "24-72h"
    assert data["valid_comparison_count"] == 0
    assert data["readiness"]["automatic_modification"] is False
    # 不能学时恰好一条状态证据，并且它如实说明原因、带得上可追溯的 id。
    assert len(data["evidence"]) == 1
    status = data["evidence"][0]
    assert status["type"] == "insufficient_sample"
    assert status["sample_count"] == 1
    assert status["comparison_group"] is None
    assert len(status["source_post_ids"]) == 1
    assert len(status["source_snapshot_ids"]) == 1
    assert status["confidence"] is None


async def test_zero_counts_are_readings_not_missing_values(session_factory):
    """计数为 0 与读不到是两件事。这里 2 次观察都是 0，所以 n=1 但**有读数**。"""
    run_id = await seed_post(
        session_factory,
        index=2,
        hook=HookType.QUESTION,
        likes=0,
        replies=0,
        reposts=0,
        quotes=0,
        snapshots=2,
    )
    graph = PerRunGraph({run_id: {"selected_topic": None, "style_recipe": recipe(HookType.QUESTION)}})

    async with learning_api(session_factory, graph) as api:
        data = (await api.get("/api/admin/learning")).json()

    check = next(c for c in data["readiness"]["checks"] if c["name"] == "metrics_present")
    # 三个分量**都**读到了 0：3 条读数，不是 0 条。
    assert "合计 3" in check["detail"]
    # 但样本量只有 1，闸门仍然正确拒绝。
    assert data["readiness"]["learning_ready"] is False
    assert data["readiness"]["reason"] == "insufficient_sample"


# ---------------------------------------------------------------------------
# 样本够的时候：可以学，但不自动改
# ---------------------------------------------------------------------------


async def test_balanced_cohorts_are_learning_ready_but_never_auto_modify(session_factory):
    graph = await seed_balanced(session_factory)

    async with learning_api(session_factory, graph) as api:
        data = (await api.get("/api/admin/learning")).json()

    assert data["available"] is True
    assert data["post_count"] == 10
    assert data["sample_count"] == 10
    assert data["readiness"]["learning_ready"] is True
    assert data["readiness"]["reason"] == "ready"
    # "可以作为学习材料" **永远不等于** "可以改 Agent"。
    assert data["readiness"]["automatic_modification"] is False
    assert data["valid_comparison_count"] == 3  # hook_type × 三个指标

    for comparison in data["comparisons"]:
        assert comparison["observation_window"] == "24-72h"
        # 观察性措辞：允许说"观察到更高"，不许说"更好"。
        assert "更好" not in comparison["statement"]
        assert comparison["group_a"] and comparison["group_b"]
        assert comparison["valid"] is True

    # 证据 = 每条成立的比较一条。这里没有"什么也没法说"的状态证据——
    # 它只在不能学时出现。
    assert len(data["evidence"]) == data["valid_comparison_count"]
    for item in data["evidence"]:
        assert item["type"] == "cohort_difference"
        assert item["comparison_group"]
        assert item["source_snapshot_ids"]
        assert item["source_post_ids"]
        assert item["source_x_post_ids"]
        assert all(x.startswith("2105998") for x in item["source_x_post_ids"])
        assert item["confidence"] is None


async def test_every_cohort_carries_sample_count_window_and_selection_rule(session_factory):
    """工作单第十一节：没有这三样，"n=5 的那组比另一组高"就无法被追问。"""
    graph = await seed_balanced(session_factory)

    async with learning_api(session_factory, graph) as api:
        data = (await api.get("/api/admin/learning")).json()

    assert data["cohort_count"] == len(data["cohorts"])
    assert data["cohort_count"] >= 2
    for cohort in data["cohorts"]:
        assert cohort["sample_count"] > 0
        assert cohort["observation_window"] == "24-72h"
        assert cohort["selection_rule"]
        assert "帖子" in cohort["selection_rule"]


async def test_a_missing_metric_stays_null_instead_of_becoming_zero(session_factory):
    """question 组的点赞**读不到**。它于是是 `null`，**不是 0**。

    补 0 会把这一组压到分布下沿，然后变成一条"question 开头表现更差"的假结论——
    而真实情况是"没读到"。比较因此必须不成立，而不是给出一个 0 对 10 的差异。
    """
    graph = await seed_balanced(session_factory, likes_for="question")

    async with learning_api(session_factory, graph) as api:
        data = (await api.get("/api/admin/learning")).json()

    attention = next(
        c
        for c in data["comparisons"]
        if c["metric"] == "attention" and c["dimension"] == "hook_type"
    )
    assert attention["valid"] is False
    assert attention["median_a"] is None
    assert attention["observed_difference"] is None
    assert attention["invalid_reason"]
    assert "读数不足" in attention["invalid_reason"]

    # 同一维度上"对话"是读得到的，所以它照样成立——缺的只是那一个指标，
    # 不是整份报告。
    conversation = next(
        c
        for c in data["comparisons"]
        if c["metric"] == "conversation" and c["dimension"] == "hook_type"
    )
    assert conversation["valid"] is True
    assert conversation["median_a"] is not None


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------


async def test_the_endpoint_is_read_only(session_factory):
    """本接口只有 GET。写方法必须由框架直接拒掉，而不是走到某个处理函数里。"""
    graph = await seed_balanced(session_factory)
    async with learning_api(session_factory, graph) as api:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = await api.request(method, "/api/admin/learning")
            assert response.status_code == 405, method


async def test_reading_the_report_does_not_touch_the_database(session_factory):
    """读两次得到逐位相同的结果：报告是现算的，不是一次写下的快照。"""
    graph = await seed_balanced(session_factory)

    async with learning_api(session_factory, graph) as api:
        first = (await api.get("/api/admin/learning")).json()
        second = (await api.get("/api/admin/learning")).json()

    assert first == second

    async with session_factory() as session:
        snapshots = (
            await session.execute(text("SELECT count(*) FROM post_metrics_snapshot"))
        ).scalar()
        posts = (await session.execute(text("SELECT count(*) FROM published_post"))).scalar()
    # 两次读没有多出任何一行快照，也没有改动帖子。
    assert snapshots == 10
    assert posts == 10


async def test_an_unknown_window_is_answered_honestly(session_factory):
    """指定一个谁都没有读数的窗口：报告如实说这个窗口是空的，不拿别的档顶替。"""
    graph = await seed_balanced(session_factory)

    async with learning_api(session_factory, graph) as api:
        data = (await api.get("/api/admin/learning", params={"window": "0-1h"})).json()

    assert data["observation_window"] == "0-1h"
    assert data["available"] is True  # 帖子是在的，只是这个窗口没有读数
    assert data["sample_count"] == 0
    assert data["readiness"]["learning_ready"] is False
    assert data["readiness"]["reason"] == "insufficient_sample"
