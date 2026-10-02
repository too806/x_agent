"""运行期取用反馈的唯一入口（Phase 5B.4）。

结构上与 `app/style/runtime.py::resolve_recipe` 完全一致，理由也一样：
候选工具要处理的不是"读一批快照"这一件事，而是**三种情况**——
"还没有任何快照"、"数据库暂时不可用"、"这次运行已经取过了"——
而**每一条路径都要拿到一个 context**（哪怕是中性的）。

把这些判断散在工具里，风险是某条提前返回路径漏掉 `feedback_context`，
于是 Writer 那一轮没有反馈可读——而这次降级是静默的，测试不会发现。
收成一个函数，工具就只负责调用它、然后无条件把结果写进 delta。

## 降级的方向：永远是可以继续跑

反馈是**增强，不是前置条件**（工作单第三十八节）。数据库读不到时返回一个
中性的空 context，运行照常进行；`format_feedback_context` 保证
`{{FEEDBACK}}` 永远有一段文本（见 `app/agent/prompts.py`），
不会留下未替换的占位符、也不会让 Writer 空手。
"""

from __future__ import annotations

import logging
from typing import Any

from app.feedback.profile import observations_from_rows
from app.feedback.report import build_feedback_context, build_report
from app.feedback.schema import FeedbackContext, FeedbackReport

logger = logging.getLogger(__name__)

#: 一次报告最多读多少条快照。
#:
#: 与 `FEEDBACK_PROFILE_READ_LIMIT` 一样是个上界，理由是运行期这条路不能被
#: 一张越来越大的快照表拖住。到达上限时会在 limitations 里写清楚
#: "这里是最近 N 条"，不让一个子集冒充全部。
FEEDBACK_SNAPSHOT_READ_LIMIT = 200


async def load_feedback_report(
    session: Any, *, limit: int = FEEDBACK_SNAPSHOT_READ_LIMIT
) -> FeedbackReport:
    """从存量快照现算一份报告。CLI 之外的读路径（Admin API）也走这里。

    **现算，不落库**：报告是观察的纯函数，存一份只会多一个将来会漂移的来源
    （同 `AiStyleSignals` / `StyleProfileFeatures` 的存放理由）。
    """
    from app import repositories

    rows = await repositories.list_recent_post_metrics_snapshots(session, limit=limit)
    report = build_report(observations_from_rows(rows))
    if len(rows) >= limit:
        report = report.model_copy(
            update={
                "limitations": (
                    *report.limitations,
                    f"可用观察超过读取上限，本报告只取了最近 {limit} 条。",
                )
            }
        )
    return report


async def resolve_feedback_context(
    session_factory: Any | None,
    *,
    existing: FeedbackContext | None = None,
    limit: int = FEEDBACK_SNAPSHOT_READ_LIMIT,
) -> FeedbackContext:
    """产出本次运行采用的反馈上下文。

    `existing` 非空时**直接复用**：一次运行里候选工具可能被调用多次
    （第一轮写三条、改稿后再补），而"这次运行看到了什么反馈"应当在整个运行里
    保持一致。每轮重读会让同一个 Run 的两批候选依据不同的历史，
    事后也就说不清是哪一批看到了什么。

    `session_factory` 为 `None`（老测试直接构造工具）或数据库不可用时，
    退化成空 context。**这不会让运行失败**：反馈是增强，不是前置条件。
    """
    if existing is not None:
        return existing

    if session_factory is None:
        return FeedbackContext()

    try:
        async with session_factory() as session:
            report = await load_feedback_report(session, limit=limit)
    except Exception:  # noqa: BLE001 - 见 docstring：反馈不该让运行失败
        logger.warning("feedback lookup failed; running with no feedback context", exc_info=True)
        return FeedbackContext()

    return build_feedback_context(report)


__all__ = [
    "FEEDBACK_SNAPSHOT_READ_LIMIT",
    "load_feedback_report",
    "resolve_feedback_context",
]
