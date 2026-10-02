"""只读装配：把数据库里的帖子变成学习报告（Phase 5C）。

这是 `app/learning/` 里**唯一**碰数据库与 checkpoint 的模块。其余五个模块是纯的，
所以"这一段逻辑对不对"能在离线 fixture 上逐位验证，而真机与离线跑的是**同一条**
从记录到报告的路（`build_learning_report`）。

## 不重复读 checkpoint

归因（`published_post` + 快照 + checkpoint）这件事在第四层已经做过一次：
`app/feedback/profile.py::build_post_performance_profile`。本模块**调用**它，
不重写一遍。两处各读一遍的代价不只是慢——两个实现迟早会在某个字段上分叉，
而那种分叉不会有任何报错，只会让同一份数据在两个界面上显示成两个样子。

## 有读取上界，而且如实说

学习要读**全部**已发布帖子，这是控制台上最重的一次读。所以有上界
（`LEARNING_POST_READ_LIMIT`），到达上界时写进 `limitations`——
"只装配了前 N 条"被读成"一共就 N 条"，会让一份残缺的报告看起来像完整的。

## 窗口是**选出来的**，不是随便一个

不指定窗口时，取读数最多的那一档（`dominant_window`）。真实数据只有 `0-1h`
的观察，默认就该是 `0-1h`；若默认成别的档，报告会因为"没人有那个窗口的读数"
而只说一句没有数据——那不是数据的问题，是默认值的问题。指定窗口时按指定的来，
哪怕那一档一条读数都没有（这时报告正确地告诉你"这个窗口什么都没有"）。
"""

from __future__ import annotations

from typing import Any

from app.feedback.profile import build_post_performance_profile
from app.feedback.schema import PostPerformanceProfile
from app.learning.performance import dominant_window, length_bucket, vector_at_window
from app.learning.report import LearningReport, build_learning_report
from app.learning.schema import PostPerformanceRecord

#: 一次学习装配最多读多少条已发布帖子。
#:
#: 取 200 是因为它远大于任何"手工能审阅完"的规模——到了这个量级，报告里的
#: 中位数与分位数本来就该由别的工具算，而不是靠一次控制台请求。到达上界时
#: 如实写进 limitations，不静默截断。
LEARNING_POST_READ_LIMIT = 200


def record_from_profile(
    profile: PostPerformanceProfile, *, window: str
) -> PostPerformanceRecord:
    """一条归因 → 一条学习记录。

    三块并排搬运，**不加权、不合成、不排序**：

    - 决策（来自 checkpoint 的当时值）：`structure_type` / `hook_type` / `stance` / …
    - 交付（来自 `published_post.body`）：`content_length` 与由它推出的 `length_bucket`；
    - 结果（来自快照表，按窗口取值）：`performance`。

    `performance` 用 `vector_at_window` 取：每个帖子在该窗口只贡献**一个**值
    （窗口内最新那条）。窗口内没有观察 → 三个分量全 `None`，**不补 0**。
    """
    return PostPerformanceRecord(
        published_post_id=profile.published_post_id,
        x_post_id=profile.x_post_id,
        published_at=profile.published_at,
        run_id=profile.run_id,
        topic=profile.topic,
        trend_score=profile.topic_trend_score,
        structure_type=profile.style_structure,
        hook_type=profile.style_hook,
        effective_target_length=profile.style_target_length,
        stance=profile.viewpoint_stance,
        counterpoint_awareness=profile.counterpoint_awareness,
        scope_exceeds_evidence=profile.scope_exceeds_evidence,
        critic_score=profile.critic_score,
        content_length=profile.content_length,
        length_bucket=length_bucket(profile.content_length),
        observation_window=window,
        performance=vector_at_window(profile.observations, window),
        observations=profile.observations,
        limitations=profile.limitations,
    )


async def load_records(
    session: Any,
    graph: Any,
    *,
    window: str | None = None,
    read_limit: int = LEARNING_POST_READ_LIMIT,
) -> tuple[tuple[PostPerformanceRecord, ...], str, tuple[str, ...]]:
    """读全部已发布帖子 → `(记录, 窗口, 装配局限)`。

    `window=None` 时用读数最多的那一档；返回值里的窗口是**实际用的那个**，
    调用方拿它去解释报告，而不是自己再猜一遍。
    """
    from app import repositories

    limitations: list[str] = []
    # 多取一条，用来**知道**是否被截断——只取 limit 条时，"正好 200 条"与
    # "有 2000 条"看起来一模一样。
    posts = await repositories.list_published_posts_with_x_id(session, limit=read_limit + 1)
    if len(posts) > read_limit:
        posts = posts[:read_limit]
        limitations.append(
            f"已发布帖子超过 {read_limit} 条，本次只装配了最早的 {read_limit} 条"
            "（按发布时间正序）；报告基于这个子集，不是全部历史。"
        )

    profiles: list[PostPerformanceProfile] = []
    for post in posts:
        profile = await build_post_performance_profile(
            session, graph, published_post_id=post.id
        )
        if profile is None:  # pragma: no cover - 刚列出来的帖子不会消失
            continue
        profiles.append(profile)

    if window is None:
        window = dominant_window([p.observations for p in profiles])

    records = tuple(record_from_profile(p, window=window) for p in profiles)
    return records, window, tuple(limitations)


async def load_learning_report(
    session: Any,
    graph: Any,
    *,
    window: str | None = None,
    read_limit: int = LEARNING_POST_READ_LIMIT,
) -> LearningReport:
    """真机入口：装配记录并产出报告。**只读，不写任何东西。**

    装配层的局限（读取上界）会被并进报告，于是"这份报告只看了多少条帖子"
    在报告里看得见，而不只存在于这个函数的返回值里。
    """
    records, resolved_window, assembly_limitations = await load_records(
        session, graph, window=window, read_limit=read_limit
    )
    report = build_learning_report(records, window=resolved_window)
    if not assembly_limitations:
        return report
    return report.model_copy(
        update={
            "limitations": tuple(
                dict.fromkeys((*assembly_limitations, *report.limitations))
            )
        }
    )


__all__ = [
    "LEARNING_POST_READ_LIMIT",
    "load_learning_report",
    "load_records",
    "record_from_profile",
]
