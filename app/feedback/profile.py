"""归因：把一条已发布帖子的**结果**与它**当时是怎么来的**并排放好（Phase 5B.4）。

工作单第十三、十四节要的是归因，不是因果。所以这里做的事是：

    结果（快照序列）  ‖  交付（当时发出去多长）  ‖  决策（当时选了哪个题、哪个观点、什么结构）

三样并排放着，**不合并、不加权、不排序**。谁都能看出"这条帖子所在的那次运行
选了 X"，但没有任何一行代码说"因为选了 X，所以点赞多"——那句话需要实验，
而我们没有实验（工作单第三十一节）。

## 两类来源，两种纪律

- **数据库里的**（快照、正文、运行记录）：直接读。这些是持久事实。
- **checkpoint 里的**（`selected_topic` 的 trend 分、`selected_angle`、
  `style_recipe`、`critic_result`）：按 run 逐条读，**读不到就是 `None`**。

第二类是这一层最容易被写坏的地方。这三个东西是**刻意不落库**的（5B.1/5B.2/5B.3
的决定：它们是运行期决策，不是资产）。所以：

- **不许**为了提高可读性给 `published_post` / 快照表加几列把它们复制下去——
  那会推翻前面三个阶段各自明确做过的决定，而且一旦两处不一致，
  谁对谁错无从判断。
- **不许**在 checkpoint 读不到时补一个默认值。"checkpoint 被回收了"与
  "当时 trend 分是 0"是两件完全不同的事，后者会被当成观测事实进入分析。
- **不许**因为读不到就让整个请求失败。读一条归因失败不该让控制台 500
  （同 `AdminService._guardrail` 的姿态）：退化成"只有数据库那部分 + 一条局限说明"。

## 每一层之间都是有意的隔离

这一层**不**重算 trend 分、**不**重跑观点评估、**不**重新打分。它读回来的都是
"当时记下来是多少"。反馈永远不会改写当前运行的趋势评分（工作单第二十七节）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from app.feedback.schema import PostPerformanceProfile, SnapshotObservation

logger = logging.getLogger(__name__)

#: 单次归因最多读多少条快照。
#:
#: 与 `AdminService.GUARDRAIL_READ_LIMIT` 同一条理由：控制台的读操作必须有一个
#: 上界，否则一条运行了很久的帖子会把整页拖住。**到达上限时如实写进
#: limitations**，而不是默默截断——否则"只看了前 50 条"会被读成"一共就这些"。
FEEDBACK_PROFILE_READ_LIMIT = 50


def observation_from_row(row: Any) -> SnapshotObservation:
    """快照行 → 只读视图。

    计数原样搬运，包括 `None`。**这里没有一处 `or 0`**：`or 0` 会把
    "X 没返回这个字段"变成"零个互动"，而那是本阶段口径上最严重的一类错误
    （工作单第十节）。
    """
    return SnapshotObservation(
        snapshot_id=str(row.id),
        x_post_id=str(row.x_post_id),
        published_post_id=str(row.published_post_id),
        observed_at=row.observed_at,
        hours_since_publish=row.hours_since_publish,
        likes=row.likes,
        replies=row.replies,
        reposts=row.reposts,
        quotes=row.quotes,
        impressions=row.impressions,
        bookmarks=row.bookmarks,
    )


def observations_from_rows(rows: Sequence[Any]) -> tuple[SnapshotObservation, ...]:
    return tuple(observation_from_row(row) for row in rows)


def _trend_score_of(state: Mapping[str, Any], selected_topic: str | None) -> float | None:
    """从**当时**的候选选题里取回选中那条的 trend 分。

    按 `title` 匹配而不是按 `topic_id`：`selected_topic` 存的是**文本**
    （下游模板与运行记录都按文本用，见 `state.py` 的说明），而候选集里
    两者都有。这是唯一能对上的键。

    取不回来就是 `None`——**不重算**。重算需要一个"现在"来算 recency，
    而"现在的趋势分"与"当时选它的时候是多少分"根本不是同一个数。
    拿后者冒充前者，会把一次历史决策的归因变成一个当下的判断。
    """
    if not selected_topic:
        return None
    for topic in state.get("candidate_topics") or []:
        if getattr(topic, "title", None) == selected_topic:
            score = getattr(topic, "trend_score", None)
            return float(score) if score is not None else None
    return None


def _enum_value(value: Any) -> str | None:
    """枚举 → 它的 `value`；不是枚举就 `str()`；空就 `None`。

    统一在这里做，免得每个字段各写一遍 `getattr(x, "value", x)`——
    那在不同字段上很容易写成不一致的形状（有的是 `None`，有的是 `""`）。
    """
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw)
    return text or None


def _counterpoint_awareness(angle: Any) -> bool | None:
    """当时这条观点有没有反方视角。

    角度缺失 → `None`；角度在但 `counterpoints` 为空列表 → `False`。
    两者的区别是**真实的**：前者是"读不到当时怎么想"，后者是"当时确实没写
    反方视角"。用 `bool(angle and angle.counterpoints)` 会把它们压成同一个值，
    于是一条真实的观察会冒充成一条缺失，或者反过来。
    """
    if angle is None:
        return None
    counterpoints = getattr(angle, "counterpoints", None)
    if counterpoints is None:
        return None
    return bool(counterpoints)


def _scope_exceeds_evidence(angle: Any) -> bool | None:
    """当时这条结论有没有被判为超出证据。读不到 → `None`（**不是 `False`**）。"""
    if angle is None:
        return None
    value = getattr(angle, "scope_exceeds_evidence", None)
    return None if value is None else bool(value)


async def _read_checkpoint(graph: Any, run_id: str) -> tuple[Mapping[str, Any] | None, str | None]:
    """读一次 checkpoint。返回 `(state, 失败原因)`。

    **任何**异常都退化成"没读到"，而不是冒泡：控制台读一条归因，不该因为
    checkpoint 被回收、或图还在写而整个 500。失败原因会进 `limitations`，
    于是"这份归因缺了一半"是界面上一句看得见的话，而不是一片沉默。
    """
    if graph is None:
        return None, "本次没有可用的运行状态读取器，归因只有数据库部分。"
    try:
        snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning("feedback profile checkpoint unavailable (run_id=%s)", run_id, exc_info=True)
        return None, "该次运行的 checkpoint 读不到（可能已被回收），归因缺少当时的决策字段。"
    values = getattr(snapshot, "values", None)
    if not values:
        return None, "该次运行的 checkpoint 为空，归因缺少当时的决策字段。"
    return values, None


async def build_post_performance_profile(
    session: Any,
    graph: Any,
    *,
    published_post_id: Any,
    read_limit: int = FEEDBACK_PROFILE_READ_LIMIT,
) -> PostPerformanceProfile | None:
    """一条已发布帖子的归因。帖子不存在 → `None`。

    `session` 只用来读（repositories 自带提交语义的读函数），本函数不写任何东西。
    """
    from app import repositories

    post = await repositories.get_published_post_by_id(session, published_post_id)
    if post is None:
        return None

    limitations: list[str] = []
    rows = await repositories.list_post_metrics_snapshots(
        session, published_post_id=post.id, limit=read_limit
    )
    observations = observations_from_rows(rows)
    if len(rows) >= read_limit:
        # `list_post_metrics_snapshots` 是**正序** + limit，取到的是最早的那批。
        # 文案必须跟着实际行为走：写成"最近的"会让人以为看到了最新状态。
        limitations.append(
            f"该帖子的观察较多，本次只读了最早的 {read_limit} 条；"
            "下面的分布基于这个子集，不是全部历史。"
        )
    if not observations:
        limitations.append("这条帖子还没有任何观察快照，因此没有结果可归因。")

    run = await repositories.get_agent_run(session, str(post.agent_run_id))
    topic = run.topic if run is not None else None
    if run is None:
        limitations.append("找不到该帖子对应的运行记录，归因缺少运行信息。")

    state, reason = await _read_checkpoint(graph, str(post.agent_run_id))
    if reason:
        limitations.append(reason)
    state = state or {}

    selected_topic = state.get("selected_topic") or None
    angle = state.get("selected_angle")
    recipe = state.get("style_recipe")
    critic = state.get("critic_result")

    return PostPerformanceProfile(
        published_post_id=str(post.id),
        x_post_id=post.x_post_id,
        published_at=post.published_at,
        # 交付长度：实际发出去的正文长度（edit 路径下这里可能是人工改写稿）。
        content_length=len(post.body) if post.body is not None else None,
        run_id=str(post.agent_run_id),
        topic=topic,
        selected_topic=selected_topic,
        topic_trend_score=_trend_score_of(state, selected_topic),
        selected_angle_claim=getattr(angle, "claim", None) if angle else None,
        selected_angle_confidence=getattr(angle, "confidence", None) if angle else None,
        viewpoint_stance=_enum_value(getattr(angle, "stance", None)),
        counterpoint_awareness=_counterpoint_awareness(angle),
        scope_exceeds_evidence=_scope_exceeds_evidence(angle),
        style_structure=_enum_value(getattr(recipe, "structure_type", None)) if recipe else None,
        style_hook=_enum_value(getattr(recipe, "hook_type", None)) if recipe else None,
        style_target_length=getattr(recipe, "effective_target_length", None) if recipe else None,
        # **当时**的评审分数。它是评审意见，不是"结果的质量"——字段名与注释
        # 都要守住这一点，否则界面会把它读成"这条帖子值 8 分"。
        critic_score=getattr(critic, "score", None) if critic else None,
        observations=observations,
        limitations=tuple(limitations),
    )


__all__ = [
    "FEEDBACK_PROFILE_READ_LIMIT",
    "build_post_performance_profile",
    "observation_from_row",
    "observations_from_rows",
]
