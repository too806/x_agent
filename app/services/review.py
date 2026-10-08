"""恢复一个人工评审：抢占 → 重放 Graph → 失败时归位。

这个模块存在的唯一理由是**人和自动审阅者必须走同一条路**。

自动发布要绕过的诱惑是"直接往 review 表插一行 approve"。那条捷径会在四个地方
塌掉，而且每一处都是静默的：

1. `PublishService._preflight` 会核对 checkpoint 里的 `final_content`
   （`publish.py:159`）。重放时节点**同时**写 review 行和 `final_content`，
   两者天然一致；直写则 `final_content` 永远不存在，那道校验被跳过——
   等于用一套更弱的保证去发布。
2. `claim_run_for_review` 是唯一把人和机器串行化的 CAS。直写要么重新实现它，
   要么就没有它；后者会重新引入 CAS 本来要防的重复 resume。
3. 直写会让 LangGraph 线程**永远停在 human_review**。本仓库没有 checkpoint GC，
   于是每次自动发布泄漏一个挂起的线程；而控制台正是从 checkpoint 读
   `human_action` / `final_content` 的（`main.py:279`），界面和数据库会永久地
   互相矛盾，且谁也说不清哪边是对的。
4. 重放天然幂等：`create_review` 是先查后写，重放不会插出第二行。

所以自动侧复刻的是**这条**路，代价是 HTTP handler 里那段失败处理必须共享出来——
也就是本模块。两条路各写一份的话，它们会在 claim 语义上悄悄漂开，
而漂开的表现是"偶尔有一条 run 谁都处理不了"，是最难查的那类 bug。
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Mapping, Protocol

from langgraph.types import Command

from app.redaction import redact
from app.repositories import (
    claim_run_for_review,
    get_agent_run,
    release_run_review_claim,
)
from app.schemas import REVIEW_ACTION_STATUS

logger = logging.getLogger(__name__)

#: Graph 里等待人工的那个节点名。用 `snapshot.next` 判断"是否仍停在等人"时要用它，
#: 所以写成常量而不是字面量散在判断里——它必须与 `app/agent/graph.py:136` 一致。
HUMAN_REVIEW_NODE = "human_review"

#: Run 停在等待人工的状态值。`claim_run_for_review` 的 expected 也用它。
WAITING_STATUS = "waiting_for_review"


class ReviewError(Exception):
    """恢复评审时的一切可预期失败。调用方按类型映射成 HTTP 码或跳过本次 tick。"""


class RunNotFound(ReviewError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"run not found: {run_id}")
        self.run_id = run_id


class RunNotWaitingForReview(ReviewError):
    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"run is not waiting for review (status={status})")
        self.run_id = run_id
        self.status = status


class RunAlreadyClaimed(ReviewError):
    """CAS 输掉了：别人（人或另一个 tick）已经抢先提交了这个 Run。"""

    def __init__(self, run_id: str) -> None:
        super().__init__("run review was already submitted")
        self.run_id = run_id


class ResumeFailed(ReviewError):
    """`ainvoke` 抛了异常。

    `released` 是关键的一位：它说明**抢到的状态有没有被放回去**。
    `False` 表示 checkpoint 其实已经推进过了（见 `resume_review` 里的判断），
    我们没有、也不该把 Run 放回 `waiting_for_review`——那会造出
    "状态说在等人、review 行却已经写了"的矛盾。这种情况下调用方**不要重试**，
    要把错误暴露出来给人看。
    """

    def __init__(
        self, run_id: str, exc: Exception, *, status: str, error: str, released: bool
    ) -> None:
        super().__init__(error)
        self.run_id = run_id
        self.exc = exc
        self.status = status
        self.error = error
        self.released = released


class ResumeRuntime(Protocol):
    """`resume_review` 需要的东西。

    用 Protocol 而不是 `from app.main import Runtime`：`app/main.py` 要 import 本模块，
    反向 import 会成环。结构类型也顺带说明了一件事——这个函数**不关心** Runtime
    还有什么别的，只要这两样。
    """

    graph: Any
    session_factory: Any


def describe_failure(run_id: str, exc: Exception) -> str:
    """记日志（脱敏）并返回可对外返回的错误描述。

    显式格式化 traceback 而不是 `logger.exception`：日志同样不允许出现凭据
    （X API key / access token 会出现在 httpx 的异常文本里）。
    """
    logger.error(
        "run failed (run_id=%s, error=%s)%s",
        run_id,
        redact(str(exc)),
        redact(traceback.format_exc()),
    )
    return redact(str(exc)) or exc.__class__.__name__


async def _still_awaiting_review(runtime: ResumeRuntime, run_id: str) -> bool:
    """Graph 是否**仍然**停在 human_review 这个节点前面。

    只在 `ainvoke` 抛异常后调用，用来区分两种长得一模一样的失败：

    - Graph 真的没动 → 放回 `waiting_for_review`，同一个决定可以重试；
    - Graph 其实已经推进了（异常发生在结果回传的路上）→ **不能放回**。
      那时 review 行已经写好、`final_content` 已经写进 checkpoint，
      放回会造出"状态说在等人、review 行却已经提交"的矛盾状态，
      而人再做一次决定会撞上 409 或者重复写一行。

    判据取 `snapshot.next` 里有没有 `human_review`：中断时 LangGraph 会把该节点
    留在"下一步要执行"的位置，图跑完则是空的。比读 `interrupts` 稳妥——
    后者在不同版本里是否被填上并不一致。
    """
    try:
        snapshot = await runtime.graph.aget_state({"configurable": {"thread_id": run_id}})
    except Exception:  # noqa: BLE001
        # 连状态都读不出来，说明 checkpointer 本身有问题。此时**按"不确定"处理**，
        # 保守地不放回：放回的代价是一条无法恢复的错状态，不放回的代价只是
        # 一条需要人看一眼的 run。方向与别处一致。
        logger.warning("resume: could not read graph state (run_id=%s)", run_id, exc_info=True)
        return False
    return HUMAN_REVIEW_NODE in tuple(getattr(snapshot, "next", ()) or ())


async def resume_review(
    runtime: ResumeRuntime,
    run_id: str,
    *,
    action: str,
    resume_value: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """把一个停在 `waiting_for_review` 的 Run 往前推一步。成功返回最终 state。

    人和自动审阅者都调它。`resume_value` 默认由 `action` 构造；自动侧传
    `{"action": "approve", "decided_by": "auto"}`——`decided_by` 走的正是
    这个"原始 dict"通道，见 `app/agent/nodes/human_review.py`。

    失败一律抛 `ReviewError` 的子类，**不返回响应对象**：HTTP 的 4xx/5xx 映射是
    HTTP 层的事，而 tick 只需要知道"这次没成，跳过"。把 FastAPI 的 Response
    塞进这一层会让 CLI 那边被迫依赖 web 框架。
    """
    if action not in REVIEW_ACTION_STATUS:
        raise ValueError(f"unknown review action: {action!r}")
    final_status = REVIEW_ACTION_STATUS[action]
    payload: Mapping[str, Any] = resume_value if resume_value is not None else {"action": action}

    session_factory = runtime.session_factory
    async with session_factory() as session:
        run = await get_agent_run(session, run_id)
    if run is None:
        raise RunNotFound(run_id)
    if run.status != WAITING_STATUS:
        raise RunNotWaitingForReview(run_id, run.status)

    # 原子抢占：并发重复提交时只有一个能拿到这个 Run。人和 tick 谁先到谁赢，
    # 输的那个拿到 RunAlreadyClaimed 而不是把同一份决定重放两遍。
    async with session_factory() as session:
        claimed = await claim_run_for_review(
            session, run_id, expected=WAITING_STATUS, new_status=final_status
        )
    if not claimed:
        raise RunAlreadyClaimed(run_id)

    logger.info("review resume requested (run_id=%s, action=%s)", run_id, action)
    try:
        # 同一个 run_id → 同一个 thread_id，从 checkpoint 恢复而不是新开线程
        return await runtime.graph.ainvoke(
            Command(resume=payload), config={"configurable": {"thread_id": run_id}}
        )
    except Exception as exc:  # noqa: BLE001
        error = describe_failure(run_id, exc)
        if not await _still_awaiting_review(runtime, run_id):
            # checkpoint 已经推进过，抢到的状态**不能再放回去**（见函数说明）。
            # 不放回意味着 Run 停在一个由 Graph 决定的状态上，而不是我们猜的状态。
            raise ResumeFailed(
                run_id, exc, status="failed", error=error, released=False
            ) from exc
        async with session_factory() as session:
            current = await release_run_review_claim(
                session, run_id, claimed_status=final_status, error=error
            )
        # current 为 None 只可能是 Run 行已不存在，此时无从判断状态，按 failed 报
        raise ResumeFailed(
            run_id, exc, status=current or "failed", error=error, released=True
        ) from exc


__all__ = [
    "HUMAN_REVIEW_NODE",
    "WAITING_STATUS",
    "ResumeFailed",
    "ResumeRuntime",
    "ReviewError",
    "RunAlreadyClaimed",
    "RunNotFound",
    "RunNotWaitingForReview",
    "describe_failure",
    "resume_review",
]
