"""发布编排：把"调用 X 发一条推文"这个**不可幂等**的动作，包进一个可恢复的状态机。

三层保证，缺一不可：

1. **预检**——本地能判定的错误一律在调用 X 之前拦下（没有审核记录、内容与审核
   记录不一致、正文超过交付硬上限、正文泄漏内部编号或含低质诱饵词）。能本地判定
   的错误不该浪费一次外部调用，更不该在外部留下痕迹。
   **长度的角色**要说清楚：`content_max_length`（`persona.max_length`，默认 140）
   是**我们自己定的交付硬上限**——不是平台的限制，我们这账号能发的比它长。它在这里
   是硬闸门，与"正文里泄漏内部编号"同级；而 60 那个推荐下限**不在这里**，
   短稿照发。见 `_preflight` 里的说明。
2. **抢占**——调用 X **之前**，在同一个事务里把 Run 从 approved/edited 抢到
   publishing 并写下发布行的在途记录（repositories.claim_publish）。
   崩溃时宁可留下"以为发出去了"的痕迹交给人工裁决，也不能没有任何痕迹：
   后者在恢复时会被当成"从没发过"而重复发推。
3. **收尾**——按 X 的实际答复分三类落库。无法判定的情况一律保守处理，
   绝不写成"确定没发出去"。

**内容的事实来源是 review 表**，不是 Graph checkpoint。checkpoint 只用来做
一致性校验（存在且不等 → 拒绝发布）。理由：review 行是人工决定落库的业务记录，
而 checkpoint 是执行器的内部状态——`claim_run_for_review` 先改 Run 状态、之后
才 resume 去写 review，两者之间存在窗口，把 checkpoint 当权威会让窗口期的
判断依赖一个还没写完的东西。

**不可消除的窗口**（必须知情）：

    [标记已提交] --网络--> [X 已创建推文] --回程--> [本地保存 post_id]
         ▲                                                    ▲
         └──────── 崩溃落在这段，本地永远不知道结果 ───────────┘

PostgreSQL 的事务管不到网络调用，X API v2 也没有幂等键。因此 exactly-once
发布在现有原语下**不可实现**。本模块把它收窄并显式化：崩溃 → 残留 publishing
→ GET 可见 → 人工裁决（POST /api/runs/{id}/publish/resolve）。不自动重试、
不自动对账。
"""
import logging
from dataclasses import dataclass
from typing import Any

from app.agent.post_structure import length_detail, over_hard_limit
from app.agent.viewpoint import evidence_index
from app.diagnostics import describe as _describe
from app.models.published_post import STATUS_FAILED, STATUS_PUBLISHED, STATUS_UNKNOWN
from app.repositories import (
    PUBLISHABLE_RUN_STATUSES,
    REVIEW_STATUS_TO_RUN_STATUS,
    claim_publish,
    confirm_published_manually,
    get_agent_run,
    get_draft_for_run,
    get_published_post,
    get_review_for_run,
    mark_publish_failed,
    mark_published,
    note_publish_unknown,
    reopen_publish_for_retry,
)
from app.services.x_api import XApiConfigError, XApiError, XApiResponseError, XApiTransportError
from app.style.gate import evaluate_content_gate

logger = logging.getLogger(__name__)

# 发布结果。调用方（HTTP 层）据此决定状态码，测试据此断言。
OUTCOME_PUBLISHED = "published"  # 确认发出去了
OUTCOME_FAILED = "failed"  # 确定没发出去，可以重试
OUTCOME_UNKNOWN = "unknown"  # 结果未知，必须人工裁决
OUTCOME_CONFLICT = "conflict"  # 当前不允许发布（已有记录 / 前置条件不满足）
OUTCOME_INVALID = "invalid"  # 内容本身不合法，本地就能判定
# 人工裁决已生效。单独一个值而不是复用 FAILED：裁决 retry 的结果状态确实是
# failed，但那是一次**成功**的操作，不能和"发布失败"共用一个词——否则 HTTP 层
# 没法区分"裁决做完了（200）"和"发布没成功（502）"。
OUTCOME_RESOLVED = "resolved"

# 允许发布的前置审核结论。reject 不在其中——被拒绝的内容绝不允许发布。
REVIEW_ACTIONS_FOR_PUBLISH = ("approve", "edit")


@dataclass(frozen=True)
class PublishResult:
    outcome: str
    status: str | None = None  # published_post.status 的最终值
    x_post_id: str | None = None
    detail: str | None = None


class PublishService:
    def __init__(self, *, x_service, session_factory, graph, content_max_length: int):
        self._x_service = x_service
        self._session_factory = session_factory
        self._graph = graph
        # 交付硬上限（`persona.max_length`）。**必填**，没有默认值：
        # 给它一个"没传就不校验"的默认值，等于把这条产品规则变成"取决于谁来构造
        # 这个对象"——而构造点越多，漏掉一处就越难发现（那种坏法的表现是
        # "有的超长稿被拦、有的发出去了"，比整齐地不拦更难查）。
        # 让它必填，漏传就是一个启动即现的 TypeError。
        self._content_max_length = content_max_length

    async def publish(self, run_id: str) -> PublishResult:
        preflight = await self._preflight(run_id)
        if isinstance(preflight, PublishResult):
            return preflight
        content, draft_id = preflight

        async with self._session_factory() as session:
            claimed = await claim_publish(
                session, agent_run_id=run_id, draft_id=draft_id, body=content
            )
        if not claimed:
            # 已经有记录且不可复用：published（发过了）或 publishing / publish_unknown
            # （结果未知）。三种都绝不能再发一次。
            return await self._describe_existing(run_id)

        logger.info("publish attempt claimed (run_id=%s)", run_id)
        return await self._call_x(run_id, content)

    # ---------- 阶段 1：纯本地预检，不写库、不调用 X ----------

    async def _preflight(self, run_id: str) -> "tuple[str, str] | PublishResult":
        async with self._session_factory() as session:
            run = await get_agent_run(session, run_id)
            review = await get_review_for_run(session, run_id)
            draft = await get_draft_for_run(session, run_id)

        if run is None:
            return PublishResult(OUTCOME_CONFLICT, detail="run not found")
        if run.status not in PUBLISHABLE_RUN_STATUSES:
            # rejected / waiting_for_review / running / failed / publishing /
            # publish_unknown / published 都不允许再次发布。
            # 带上已有记录：调用方需要知道"为什么不能发"——是已经发过了，还是
            # 结果未知需要人工裁决。两者都是 409，但处置方式完全不同。
            return await self._conflict(
                run_id, f"run is not publishable (status={run.status})"
            )
        if draft is None:
            return PublishResult(OUTCOME_CONFLICT, detail="run has no draft")
        if review is None or review.status not in REVIEW_ACTIONS_FOR_PUBLISH:
            # 把关审核结果本身：没有审核记录、或结论是 reject，都不允许发布。
            # 这个分支同时覆盖了"review 刚抢到状态、resume 还没写完 review 行"的窗口。
            return PublishResult(
                OUTCOME_CONFLICT,
                detail=f"run has no approving review (review={getattr(review, 'status', None)})",
            )

        # Run 状态与审核结论必须互相钉死。两者不一致只可能是一个窗口：
        # claim_run_for_review 已把 Run 改成新状态，而 resume 还没跑到 create_review
        # 去覆写**上一次残留**的 review 行。此时 review 里的内容是过期的决定，
        # 照它发布就会发出一个人这次并没有批准过的东西。
        if run.status != REVIEW_STATUS_TO_RUN_STATUS[review.status]:
            return PublishResult(
                OUTCOME_CONFLICT,
                detail=(
                    f"run.status={run.status} does not match review={review.status} "
                    "(review may still be resuming)"
                ),
            )

        # 正文取自 review 表——它是人工决定的业务事实来源。
        # 刻意**不读** draft.body：edit 路径下 draft 存的是原稿，拿它发布等于
        # 发布了没人批准过的内容。
        content = review.original_content if review.status == "approve" else review.edited_content
        if not isinstance(content, str) or not content.strip():
            return PublishResult(OUTCOME_CONFLICT, detail="review has no publishable content")

        # checkpoint 只做一致性校验，**不是事实来源**：
        # - 没有值 → 放行。"checkpoint 还没写"正是上面那条窗口的正常表现，而 review
        #   已经是权威记录，此时拒绝反而会让一个正常完成的审核无法发布。
        # - 有值但不等 → 拒绝。checkpoint 与数据库分叉了，宁可停下。
        state = await self._graph.aget_state({"configurable": {"thread_id": run_id}})
        final_content = (state.values or {}).get("final_content")
        if isinstance(final_content, str) and final_content != content:
            return PublishResult(
                OUTCOME_CONFLICT,
                detail="checkpoint final_content does not match the recorded review decision",
            )

        # 交付硬上限：超过 `content_max_length` 的正文一律不发。
        #
        # 这道闸门走过一个来回，把两次的理由都记在这里，免得下次又凭一半的记忆
        # 把它改回去：
        #
        # - 起初它在，理由是"能本地判定就别浪费一次外部调用去换 X 的 400"。那个
        #   理由本身是错的（假想敌是 X 的 400，而 X 其实收得下更长的稿子）。
        # - 后来撤掉，理由是"`max_length` 是我们自己的**写作目标**，不是平台限制"。
        #   这句话没错，但**撤错了地方**：它是写作目标这一点，说明它不该被当成
        #   平台限制去呈现，不说明它不该被当成交付条件去执行。撤掉之后，"这条稿
        #   比目标长了 40 字"只剩一条 `guardrail` 提示，而提示是**可以放着不管的**
        #   ——于是超长稿一路走到发布。
        # - 现在它在，理由只有一条：这是我们要的交付标准。到发布这一步，正文已经
        #   经过 Agent 的候选选择、Critic、人类审核三道关；还超长说明这三道关都
        #   没拦住，那么**在最后一次机会上拦住它就是唯一诚实的做法**。
        #
        # 特别注意 `edit` 路径：人工改写稿也走这里。人可以手改正文（那正是 edit 的
        # 用途），但不能用改长来绕过上限——否则"过不了就手工改"会变成默认操作，
        # 与内容门禁一视同仁的那条纪律就自相矛盾了。
        #
        # 为什么用 `OUTCOME_INVALID` 而不是新造一个结果：它与内容门禁是同一类
        # 事实——"这条正文不能发，不是网络问题、不是状态问题"，调用方对这两者的
        # 处置也相同（回去改正文）。顺带：这里返回等于**没有抢占过**，Run 状态与
        # 发布行都不动，所以它不会制造 publishing 残留，也不会落进"结果未知"。

        if over_hard_limit(content, self._content_max_length):
            # 用 `length_detail` 而不是手写 f-string：这条判据在三个地方被用
            # （Critic 的确定性拦截、finalize 的选稿、这里），谁改了长度口径，
            # 三处的说法会一起变——而不是一处改了、另两处继续按老口径说话。
            return PublishResult(
                OUTCOME_INVALID,
                detail=(
                    f"{length_detail(content, self._content_max_length)}；"
                    "超过交付硬上限的正文不允许发布，请先把它改短"
                ),
            )

        # 内容门禁：正文里泄漏内部事实编号、或出现低质点击诱饵词，一律不发。
        # 与 guardrail 调的是**同一个函数**（`evaluate_content_gate`），所以
        # 审核时看到的理由与这里拦下的理由不会漂开。区别只在于：guardrail 把
        # 结论写进 reason 交给人工，这里直接 422。
        #
        # 门禁对 approve 与 edit **一视同仁**：人编辑过的正文也走同一道闸门。
        # 否则"过不了门禁就手工编辑绕过"会成为默认操作，门禁也就形同虚设。
        # 人的出口是把正文改到合规，而不是让系统忘掉这条规则。
        violations = evaluate_content_gate(content, fact_ids=self._fact_ids_of(state))
        if violations:
            return PublishResult(
                OUTCOME_INVALID,
                detail="；".join(violation.detail for violation in violations),
            )

        return content, str(draft.id)

    @staticmethod
    def _fact_ids_of(state: Any) -> frozenset[str] | None:
        """从这次 Run 的 checkpoint 里取本轮事实编号，供门禁核对裸形编号。

        取不到证据包（V1 链路、或 checkpoint 还没写）时返回 `None`——门禁据此
        只启用零假阳性的括号形，而不是把一次核对不了当成"没有编号"。
        """
        pack = (getattr(state, "values", None) or {}).get("evidence_pack")
        return evidence_index(pack).fact_ids or None

    # ---------- 阶段 3：按 X 的答复收尾 ----------

    async def _call_x(self, run_id: str, content: str) -> PublishResult:
        try:
            post_id = await self._x_service.publish(content)
        except XApiConfigError as exc:
            # 在任何 HTTP 请求之前抛出 → 确定没发出去，可以重试
            return await self._fail(run_id, _describe(exc))
        except XApiResponseError as exc:
            detail = _describe(exc)
            if 400 <= exc.status_code < 500:
                # X 明确拒绝（含 402 / 429）→ 没有创建内容
                return await self._fail(run_id, detail)
            # 5xx、或 2xx 但拿不到 id：请求已被处理，结果不可判定
            return await self._unknown(run_id, detail)
        except XApiTransportError as exc:
            detail = _describe(exc)
            if exc.may_have_reached_x:
                return await self._unknown(run_id, detail)
            return await self._fail(run_id, detail)
        except XApiError as exc:
            # 兜底：不认识的 X 错误一律按"可能发出去了"处理。这是唯一安全的方向。
            return await self._unknown(run_id, _describe(exc))

        async with self._session_factory() as session:
            applied = await mark_published(session, agent_run_id=run_id, x_post_id=post_id)
        if not applied:  # pragma: no cover - 只有人工在这瞬间裁决过才可能发生
            logger.warning("published on X but local record was already settled (run_id=%s)", run_id)
            return await self._describe_existing(run_id)
        logger.info("published (run_id=%s, x_post_id=%s)", run_id, post_id)
        return PublishResult(OUTCOME_PUBLISHED, status=STATUS_PUBLISHED, x_post_id=post_id)

    async def _fail(self, run_id: str, detail: str) -> PublishResult:
        async with self._session_factory() as session:
            await mark_publish_failed(session, agent_run_id=run_id, error=detail)
        logger.warning("publish failed, retryable (run_id=%s, detail=%s)", run_id, detail)
        return PublishResult(OUTCOME_FAILED, status=STATUS_FAILED, detail=detail)

    async def _unknown(self, run_id: str, detail: str) -> PublishResult:
        """尝试已经结束，但我们无法判定结果。

        落成 publish_unknown 而不是留在 publishing：两者都进人工裁决、都不自动
        重试，但 publishing 表示"本地一无所知（可能还在发）"，而这里我们确定
        这次尝试已经结束了。区分开，运维才知道该不该再等。
        """
        async with self._session_factory() as session:
            await note_publish_unknown(session, agent_run_id=run_id, error=detail)
        logger.warning("publish result unknown, manual review needed (run_id=%s)", run_id)
        return PublishResult(OUTCOME_UNKNOWN, status=STATUS_UNKNOWN, detail=detail)

    async def _conflict(self, run_id: str, detail: str) -> PublishResult:
        """冲突结果：若已有发布记录就一并带回。

        "已经有记录"和"压根不能发"对调用方是两件事——前者要去看 publish.status
        决定是人工裁决还是收手，后者只需修正请求。只给一个 409 是不够的。
        """
        async with self._session_factory() as session:
            row = await get_published_post(session, run_id)
        if row is None:
            return PublishResult(OUTCOME_CONFLICT, detail=detail)
        return PublishResult(
            OUTCOME_CONFLICT, status=row.status, x_post_id=row.x_post_id, detail=detail
        )

    async def _describe_existing(self, run_id: str) -> PublishResult:
        async with self._session_factory() as session:
            row = await get_published_post(session, run_id)
        if row is None:  # pragma: no cover - 抢占失败却没有行是不该发生的
            return PublishResult(OUTCOME_CONFLICT, detail="publish already in progress")
        return PublishResult(
            OUTCOME_CONFLICT,
            status=row.status,
            x_post_id=row.x_post_id,
            detail=f"a publish record already exists (status={row.status})",
        )

    # ---------- 人工裁决 ----------

    async def resolve(
        self, run_id: str, *, action: str, x_post_id: str | None = None
    ) -> PublishResult:
        """人工裁决"结果未知"的发布。

        只负责**纠正状态**，不在这里发帖：真正重发必须另外显式再调一次
        POST /publish。误触的代价因此从"多一条推文"降为"状态改错（还能改回来）"。
        """
        if action == "mark_published" and not x_post_id:
            raise ValueError("x_post_id is required for mark_published")

        async with self._session_factory() as session:
            if action == "mark_published":
                changed = await confirm_published_manually(
                    session, agent_run_id=run_id, x_post_id=x_post_id
                )
                settled = STATUS_PUBLISHED
            else:
                changed = await reopen_publish_for_retry(session, agent_run_id=run_id)
                settled = STATUS_FAILED

        if not changed:
            return await self._describe_existing(run_id)
        return PublishResult(OUTCOME_RESOLVED, status=settled, x_post_id=x_post_id)
