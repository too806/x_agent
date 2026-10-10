"""无人值守自动发布：一次 tick 的编排。

    enabled=false ─→ 什么都不做（唯一的刹车，改配置文件立即生效）
    enabled=true  ─→ 抢锁 → 收残留 → 查额度 → 建 Run → 跑 Graph → 判据
                                        ├─ 影子：记录，到此为止
                                        └─ 正式：通过才 resume + publish

## 这个模块**不做**判断

判断全部在 `app/services/auto_review.py`（纯函数，零 IO）。这里只负责
"什么时候问它"和"问完之后动作还是不动"——把两件事分开，是因为判据要被单测
逐条钉住，而编排要被竞态测试钉住，让它们共处一个文件会让两种测试互相绊住。

## 完整复用人走的那条路

建 Run 用的 `create_agent_run`、resume 用的 `resume_review`、发布用的
`PublishService` —— 全是人工路径上的**同一个函数**。自动侧没有任何一条私有的
写库路径。这不是洁癖：两条路各写一份，迟早会漂开，而漂开的表现是
"手动发没问题、自动发偶尔卡住"，是最难查的那类bug。理由的完整版见
`app/services/review.py` 的模块说明。

## 一条刻意的不对称

    判据通过  → 才真正动作（正式模式发帖、占额度）
    判据不通过 → **什么都不做**，Run 留在 `waiting_for_review`，带理由进人工队列

一次错误的拒绝只多一条队列记录；**一次错误的批准是一条已经发出去的烂帖**。
所以自动闸门是"快车道"，不是"判决者"——它从不自动**拒绝**任何东西。

## 硬约束（既有设计，本模块不得违背）

- 绝不自动裁决 `publish_unknown`——那是人的活
- 绝不自动重试失败的发布
- 绝不自动拒绝
- 任何"清理/收尾"查询都必须限定在自己发起的 Run 上

## 额度是**两个**

- `max_auto_runs_per_day`：管**成本与队列**。影子模式下一条都不发，但每个 tick
  都会跑一整轮 Agent（最多 12 步 LLM + 真实 X 检索），所以它在影子模式下
  同样必须生效——否则影子模式会变成一台烧钱机器。
- `max_publishes_per_day`：管**对外可见的产出**。计入**尝试数**，因为
  `publish_unknown` 的那次可能已经发出去了（见 `count_publish_attempts_since`）。

## 时间口径

"今天"按 **UTC** 算，与数据库里存的时区一致。对中国时区的使用者来说，
额度在北京时间早上 8 点重置而不是零点——这是刻意的取舍：按本地日历算需要在
每次查询前做一次时区换算，而那个换算一旦写错，表现是"额度偶尔少算一天"，
比"重置时刻不是零点"难查得多。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from app.agent.viewpoint import evidence_index
from app.models.agent_run import ORIGIN_SCHEDULED
from app.models.review import DECIDED_BY_AUTO
from app.repositories import (
    count_auto_approvals_with_gate_violations,
    count_auto_runs_since,
    count_publish_attempts_since,
    count_stale_review_claims,
    create_agent_run,
    earliest_auto_review_at,
    insert_auto_review,
    list_false_reject_reasons,
    list_recent_draft_contents,
    list_recent_published_contents,
    list_recent_topics,
    mark_auto_review_acted,
    mark_run_completed,
    mark_run_failed,
    mark_run_waiting_for_review,
    reap_stale_review_claims,
    release_auto_publish_lock,
    shadow_agreement_stats,
    try_auto_publish_lock,
)
from app.schemas import (
    AutoPublishAgreement,
    AutoPublishGate,
    AutoPublishGateCriterion,
    AutoPublishStatus,
)
from app.services.auto_review import (
    evaluate_auto_publish,
    evaluate_go_live_gate,
    summarize_agreement,
)
from app.services.automation_config import AutomationConfig, load_automation_config
from app.services.publish import OUTCOME_PUBLISHED, OUTCOME_UNKNOWN
from app.services.review import ReviewError, describe_failure, resume_review

logger = logging.getLogger(__name__)

#: 累计计数用的下界。`count_auto_runs_since` 需要一个 since，而"从有史以来"
#: 用纪元起点表达。刻意不为此再写一个 `count_auto_runs()`——两个只差一个 where
#: 的函数迟早会有一边被改漏。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

OUTCOME_DISABLED = "disabled"
OUTCOME_CONFIG_ERROR = "config_error"
OUTCOME_LOCKED = "locked"
OUTCOME_RUN_CAP = "run_cap"
OUTCOME_NO_DRAFT = "no_draft"
OUTCOME_ERROR = "error"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_REVIEWED = "reviewed"


@dataclass
class TickResult:
    """一次 tick 的结构化结果。CLI 直接打印它，测试直接断言它。

    带 `run_id` 而不是只给一个字符串结论：出问题时要能立刻跳到那条 Run 上去看，
    而"这次 tick 判据没过"本身不含任何可操作的线索。
    """

    outcome: str
    mode: str | None = None
    run_id: str | None = None
    topic: str | None = None
    verdict: str | None = None
    approved: bool = False
    published: bool = False
    reasons: list[str] = field(default_factory=list)
    reaped: list[str] = field(default_factory=list)
    runs_today: int = 0
    publishes_today: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "mode": self.mode,
            "run_id": self.run_id,
            "topic": self.topic,
            "verdict": self.verdict,
            "approved": self.approved,
            "published": self.published,
            "reasons": self.reasons,
            "reaped": self.reaped,
            "runs_today": self.runs_today,
            "publishes_today": self.publishes_today,
            "error": self.error,
        }


class TickRuntime(Protocol):
    graph: Any
    session_factory: Any
    publish_service: Any


def _utc_day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


class AutoPublishService:
    """每个 tick 调用一次 `tick()`。**没有常驻状态**——所有状态都在库里。

    无状态是刻意的：调度器是外部进程（Windows 计划任务），每次运行都是一个
    全新的 Python 进程。任何存在实例上的计数器都会在两次 tick 之间消失，
    于是额度形同虚设。
    """

    def __init__(self, runtime: TickRuntime, *, config_path: str | None = None) -> None:
        self._runtime = runtime
        self._config_path = config_path

    async def tick(self, *, now: datetime | None = None) -> TickResult:
        """跑一轮。**不抛异常**——一切失败都折进 `TickResult.outcome`。

        这条契约是给调度器用的：计划任务每 N 分钟拉起一个新进程，
        如果这里抛异常，进程以非 0 退出，而下一次还会照常拉起——失败因此
        只在事件日志里留下几百行一样的 traceback，没人在看。
        把结论放进结构化结果、由 CLI 打印并决定退出码，至少每一轮都有一行
        说清楚发生了什么。
        """
        moment = now or datetime.now(timezone.utc)

        # --- 1. 重读配置（热杀开关）-----------------------------------------
        #
        # 每个 tick 读一次文件，是"改配置立即生效"的唯一实现方式。配置写坏时
        # 跳过本轮而不是崩掉：一个 YAML 缩进错误不该让无人值守的东西静默停摆。
        try:
            config = load_automation_config(self._config_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto publish: config load failed")
            return TickResult(outcome=OUTCOME_CONFIG_ERROR, error=str(exc))

        if not config.enabled:
            logger.info("auto publish: disabled")
            return TickResult(outcome=OUTCOME_DISABLED, mode=config.mode)

        # --- 2. 抢锁 ---------------------------------------------------------
        #
        # 单独一条会话持有锁，跨越整个 tick（包括下面的 resume / publish，
        # 它们各自用自己的会话）。锁是会话级的，只要这条会话不关就一直在。
        async with self._runtime.session_factory() as lock_session:
            if not await try_auto_publish_lock(lock_session):
                logger.info("auto publish: another tick holds the lock")
                return TickResult(outcome=OUTCOME_LOCKED, mode=config.mode)
            try:
                return await self._run_tick(config, moment)
            finally:
                # 必须显式解锁，不能靠连接关闭：连接会带着锁回到 SQLAlchemy 的
                # 池子里，下一个拿到它的人会继承一把已经"被占用"的锁——一次漏放
                # 会让后续所有 tick 永久静默失效。见 repositories 里的说明。
                await release_auto_publish_lock(lock_session)

    async def _run_tick(self, config: AutomationConfig, moment: datetime) -> TickResult:
        day_start = _utc_day_start(moment)
        result = TickResult(outcome=OUTCOME_REVIEWED, mode=config.mode)

        async with self._runtime.session_factory() as session:
            # --- 3. 收残留 ---------------------------------------------------
            #
            # 上一次 tick 若崩在 claim 之后、resume 之前，会留下一条
            # "状态是 approved 但没有 review 行"的 Run：人审它得 409，
            # 发布它也 409，谁都处理不了。这里把它放回 waiting_for_review。
            #
            # 只收自动发起的 Run——理由见 reap_stale_review_claims 的说明。
            result.reaped = await reap_stale_review_claims(session)
            if result.reaped:
                logger.warning("auto publish: reaped stale claims %s", result.reaped)

            # --- 4. 额度：在**建 Run 之前**查 ----------------------------------
            #
            # 顺序是有意义的。放在建 Run 之后的话，每一轮超额的 tick 仍然会付掉
            # 一整轮 Agent 的钱（12 步 LLM + X 检索）才发现该停。
            result.runs_today = await count_auto_runs_since(session, since=day_start)
            if result.runs_today >= config.max_auto_runs_per_day:
                logger.info(
                    "auto publish: daily run cap reached (%s/%s)",
                    result.runs_today,
                    config.max_auto_runs_per_day,
                )
                result.outcome = OUTCOME_RUN_CAP
                return result

            result.publishes_today = await count_publish_attempts_since(
                session, since=day_start
            )
            total_runs = await count_auto_runs_since(session, since=_EPOCH)

            goal = config.choose_goal(total_runs)
            run = await create_agent_run(
                session, None, goal=goal, origin=ORIGIN_SCHEDULED
            )
            run_id = str(run.id)

        result.run_id = run_id
        logger.info(
            "auto publish: run started (run_id=%s, mode=%s, goal=%r)",
            run_id,
            config.mode,
            goal,
        )

        # --- 5. 跑 Graph ------------------------------------------------------
        try:
            final_state = await asyncio.wait_for(
                self._runtime.graph.ainvoke(
                    {"run_id": run_id, "goal": goal},
                    config={"configurable": {"thread_id": run_id}},
                ),
                timeout=config.tick_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # `llm_timeout_seconds` 只约束**单次**调用，约束不了整轮（一轮最多
            # 12 步）。超时把 Run 记成 failed：这是诚实的——Agent 没跑完，
            # 没有可以审的东西。**不做任何清理**，checkpoint 留在哪儿就是哪儿，
            # 下一次 tick 的 reaper 只看自动 Run 的 claim 残留，不会碰它。
            message = f"auto tick timed out after {config.tick_timeout_seconds}s"
            logger.error("auto publish: %s (run_id=%s)", message, run_id)
            async with self._runtime.session_factory() as session:
                await mark_run_failed(session, run_id, message)
            result.outcome = OUTCOME_TIMEOUT
            result.error = message
            return result
        except Exception as exc:  # noqa: BLE001
            message = describe_failure(run_id, exc)
            async with self._runtime.session_factory() as session:
                await mark_run_failed(session, run_id, message)
            result.outcome = OUTCOME_ERROR
            result.error = message
            return result

        if "__interrupt__" not in final_state:
            # 没有停到 human_review，说明没走到那一步（正常路径不可能到这里，
            # finalize 产出 draft 之后必然进 guardrail → human_review）。
            # 记 completed 并如实报告，不去猜原因。
            logger.warning("auto publish: run did not reach review (run_id=%s)", run_id)
            async with self._runtime.session_factory() as session:
                await mark_run_completed(session, run_id)
            result.outcome = OUTCOME_NO_DRAFT
            return result

        # 停在 human_review：先把 Run 推进等待审核，与人工路径完全一致。
        async with self._runtime.session_factory() as session:
            await mark_run_waiting_for_review(session, run_id)

        state = dict(_state_values(final_state))
        result.topic = state.get("selected_topic")

        return await self._review_and_maybe_act(config, result, run_id, state)

    async def _review_and_maybe_act(
        self,
        config: AutomationConfig,
        result: TickResult,
        run_id: str,
        state: Mapping[str, Any],
    ) -> TickResult:
        draft_id = state.get("draft_id")
        content = state.get("draft_content")

        # 长度上限从运行时取，**不在这里另设一个数**：它必须与发布侧
        # （`PublishService` 的 `content_max_length`）和 finalize 用同一个值，
        # 否则会出现"这里放行、那里拦下"。`getattr` 兜底只是为了让没有 persona
        # 的测试替身仍然能跑——真那个数缺失时下面的判据会如实记下"没判"，
        # 而不是悄悄按 0 或 140 去判。
        persona = getattr(self._runtime, "persona", None)
        max_length = getattr(persona, "max_length", None)

        decision = None
        try:
            decision = evaluate_auto_publish(
                content=content,
                state=state,
                guardrail_reasons=state.get("guardrail_reasons") or [],
                fact_ids=evidence_index(state.get("evidence_pack")).fact_ids,
                recent_contents=await self._recent_contents(config, draft_id),
                recent_topics=await self._recent_topics(config, run_id),
                publish_attempts_today=result.publishes_today,
                config=config,
                max_length=max_length,
            )
        except Exception as exc:  # noqa: BLE001
            # 判据本身抛异常 = 出现了没预料到的输入形状。**按不通过处理**，
            # 让 Run 留在人工队列里，而不是让一次 tick 崩掉整个调度循环。
            message = f"auto review failed to evaluate: {exc}"
            logger.exception("auto publish: %s (run_id=%s)", message, run_id)
            result.outcome = OUTCOME_ERROR
            result.error = message
            return result

        result.verdict = decision.verdict
        result.approved = decision.approved
        result.reasons = list(decision.reasons)

        async with self._runtime.session_factory() as session:
            # ON CONFLICT DO NOTHING：同一个 draft 被 tick 重放时不会插出第二行。
            # 返回 None 表示"这一条已经判过了"，不是错误——下一行的 acted 标记
            # 也就不会再被翻一次。
            await insert_auto_review(
                session,
                agent_run_id=run_id,
                draft_id=str(draft_id) if draft_id else None,
                mode=config.mode,
                verdict=decision.verdict,
                reasons=list(decision.reasons),
                checks=decision.checks,
                critic_score=decision.critic_score,
            )

        if not decision.approved:
            # 不通过 → **什么都不做**。Run 留在 waiting_for_review 进人工队列，
            # 理由已经写进 auto_review 行，控制台能显示出来。
            logger.info(
                "auto publish: held for human (run_id=%s, reasons=%s)",
                run_id,
                decision.reasons,
            )
            return result

        if config.is_shadow:
            # 影子模式：判据照跑、结论照记，**但一条都不发**。人仍然照常审。
            # 这一条就是"影子"的全部含义——差别只在通过这一侧，不通过时
            # 两种模式的行为完全相同。
            logger.info("auto publish: shadow verdict approve (run_id=%s)", run_id)
            return result

        return await self._publish(result, run_id, draft_id)

    async def _publish(
        self, result: TickResult, run_id: str, draft_id: Any
    ) -> TickResult:
        """正式模式：resume + 发布。**任何失败都不重试。**

        resume 失败（`ReviewError`）直接返回：Run 停在什么状态就是什么状态，
        由人接手。重试会撞上"状态说在等人、review 行却已写好"的矛盾，
        而 `ResumeFailed.released=False` 那种情况重试更是明确有害。
        """
        try:
            await resume_review(
                self._runtime,
                run_id,
                action="approve",
                # `decided_by` 走的是**原始 resume dict** 这条通道，故意不经过
                # 决策 schema——见 app/agent/nodes/human_review.py::_decided_by_of。
                resume_value={"action": "approve", "decided_by": DECIDED_BY_AUTO},
            )
        except ReviewError as exc:
            logger.error("auto publish: resume failed (run_id=%s): %s", run_id, exc)
            result.error = str(exc)
            return result

        # 发布**不重试**：X 的错误在 PublishService 内部已经分类落库，
        # `publish_unknown` 要人裁决。这里只如实记录结果。
        publish_result = await self._runtime.publish_service.publish(run_id)
        outcome = getattr(publish_result, "outcome", None)
        result.published = outcome == OUTCOME_PUBLISHED
        if not result.published:
            logger.warning(
                "auto publish: publish did not confirm (run_id=%s, outcome=%s)",
                run_id,
                outcome,
            )

        # 只在**可能真的发出去了**之后置位。`acted` 的语义是"自动审阅者发了这条"，
        # 不是"判据通过了"，也不是"尝试过发布"——混起来的话，影子模式下的记录会
        # 看起来像是已经发布过，而一次确定失败的发布（402 / 内容不合法 / 冲突）
        # 会让 Run 详情上写着"是否真的动作：是"，X 上却什么都没有。
        # `publish_unknown` 要算 True：那次请求可能已经送达，这正是它要人裁决的原因。
        if draft_id is not None and outcome in (OUTCOME_PUBLISHED, OUTCOME_UNKNOWN):
            async with self._runtime.session_factory() as session:
                await mark_auto_review_acted(session, draft_id=str(draft_id))
        logger.info("auto publish: published (run_id=%s)", run_id)
        return result

    async def _recent_contents(
        self, config: AutomationConfig, draft_id: Any
    ) -> list[str]:
        """重复检测的语料：近期**已发布**正文 + 近期**草稿**正文。

        后半段是影子模式能成立的前提。影子模式什么都不发，`published_post`
        始终为空——只喂已发布正文的话，检测器拿到的是一份空语料，于是
        **放行一切**，量出来的一致率毫无意义（一条永远说"通过"的橡皮图章）。
        见 `repositories.list_recent_draft_contents` 的说明。
        """
        since = _window_start(config)
        async with self._runtime.session_factory() as session:
            published = await list_recent_published_contents(
                session, since=since, limit=config.recent_corpus_limit
            )
            drafts = await list_recent_draft_contents(
                session,
                since=since,
                limit=config.recent_corpus_limit,
                exclude_draft_id=str(draft_id) if draft_id else None,
            )
        return [*published, *drafts]

    async def _recent_topics(self, config: AutomationConfig, run_id: str) -> list[str]:
        """近期用过的题目，**排除当前这条 Run**。

        当前 Run 的题目在 `finalize` 里已经写进 `agent_run.topic` 了，
        不排除它就会变成"拿这条稿子的题目跟这条稿子的题目比"，
        于是每一次自动运行都会因重复被判不通过。见
        `repositories.list_recent_topics` 的说明。
        """
        async with self._runtime.session_factory() as session:
            return await list_recent_topics(
                session,
                since=_window_start(config),
                limit=config.recent_corpus_limit,
                exclude_run_id=run_id,
            )


def _window_start(config: AutomationConfig, now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment - timedelta(days=config.recent_window_days)


def _state_values(final_state: Mapping[str, Any]) -> Mapping[str, Any]:
    """从 `ainvoke` 的返回值里取 state 本身。

    中断时返回的 dict 是 **state 的扁平副本 + 一个 `__interrupt__` 键**
    （见 `app/main.py` 里 `final_state["__interrupt__"][0].value` 的用法），
    所以它本身就是 state，直接返回。写成函数是为了给这件事一个名字——
    将来 LangGraph 改了形状，要改的是这一处，不是每个调用点。
    """
    return final_state


def build_auto_publish_service(runtime: TickRuntime, *, config_path: str | None = None):
    return AutoPublishService(runtime, config_path=config_path)


#: 转正式门槛读不懂的那些东西，如实写在响应里。
#:
#: 这几条不是免责声明，是**边界说明**：它们决定了 `ready=true` 这句话
#: 到底覆盖了什么。尤其第一条——本层测不出语义改写，而语义改写恰好是
#: 最可能发生的重复形式。
STATUS_LIMITATIONS: tuple[str, ...] = (
    "重复检测测不出真·语义改写、同一个主张换一批证据、以及"
    "「同一事件换个角度」（选题串不同）。项目禁 ML 依赖、不做 embedding，"
    "这几样就是测不了，靠影子模式加人兜。",
    "一致率只统计**已决**的配对，而且只数**人**的决定（`decided_by='human'`）。"
    "人还没审的那些不计入分母，所以这个数不反映「积压了多少没审」；"
    "正式模式下机器自己替自己点的那次「批准」同样不计入——否则就是拿机器的"
    "判断给机器打分。",
    "观察窗口从库里第一条自动结论算起，不是从配置文件最后一次改动算起——"
    "文件 mtime 会被编辑器、备份还原、git checkout 随意改动。",
    "`ready` 只覆盖机器算得出的判据；`manual_steps` 那几步机器判断不了，"
    "每次都会原样下发。",
)

_MAPPING_NOTE = (
    "人 approve = 原样接受；人 edit = 不会原样接受"
    "（**正式模式发的是未编辑的原稿**，所以 edit 计为 soft_mismatch 而不是一致）；"
    "人 reject = 拦下。"
)


async def collect_status(
    session_factory, *, config_path: str | None = None, now: datetime | None = None
) -> AutoPublishStatus:
    """组装 `GET /api/admin/auto-publish/status` 的响应。

    配置读不了时**不报错**：`agreement` 与 `gate` 算的是库里已有的数据，
    与配置文件能不能解析无关，因为它们恰恰是"要不要改配置"的依据。
    配置那几个字段按安全默认值下发，并把错误原样带出来。
    """
    config_error: str | None = None
    try:
        config = load_automation_config(config_path)
    except Exception as exc:  # noqa: BLE001
        config = AutomationConfig()
        config_error = str(exc)

    moment = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        counts = await shadow_agreement_stats(session)
        earliest = await earliest_auto_review_at(session)
        stale_claims = await count_stale_review_claims(session)
        gate_violations = await count_auto_approvals_with_gate_violations(session)
        false_reject_reasons = await list_false_reject_reasons(session)

    summary = summarize_agreement(counts)
    window_days = (
        (moment - earliest).total_seconds() / 86400.0 if earliest is not None else 0.0
    )
    report = evaluate_go_live_gate(
        summary,
        window_days=window_days,
        false_reject_reasons=false_reject_reasons,
        stale_claims=stale_claims,
        auto_approved_clean=gate_violations == 0,
    )

    return AutoPublishStatus(
        enabled=config.enabled,
        mode=config.mode,
        goals=list(config.goals),
        max_auto_runs_per_day=config.max_auto_runs_per_day,
        max_publishes_per_day=config.max_publishes_per_day,
        min_critic_score=config.min_critic_score,
        recent_window_days=config.recent_window_days,
        config_error=config_error,
        agreement=AutoPublishAgreement(
            decided_pairs=summary.decided_pairs,
            agreed=summary.agreed,
            false_approve=summary.false_approve,
            soft_mismatch=summary.soft_mismatch,
            false_reject=summary.false_reject,
            auto_approvals=summary.auto_approvals,
            agreement_rate=summary.agreement_rate,
            soft_mismatch_rate=summary.soft_mismatch_rate,
        ),
        gate=AutoPublishGate(
            ready=report.ready,
            criteria=[
                AutoPublishGateCriterion(
                    key=c.key, label=c.label, met=c.met, detail=c.detail
                )
                for c in report.criteria
            ],
            manual_steps=list(report.manual_steps),
        ),
        mapping_note=_MAPPING_NOTE,
        limitations=list(STATUS_LIMITATIONS),
    )


__all__ = [
    "OUTCOME_CONFIG_ERROR",
    "OUTCOME_DISABLED",
    "OUTCOME_ERROR",
    "OUTCOME_LOCKED",
    "OUTCOME_NO_DRAFT",
    "OUTCOME_REVIEWED",
    "OUTCOME_RUN_CAP",
    "OUTCOME_TIMEOUT",
    "STATUS_LIMITATIONS",
    "AutoPublishService",
    "TickResult",
    "build_auto_publish_service",
    "collect_status",
]
