"""finalize：把 Agent 的产物翻译成 V1 所要求的 `draft_content` / `draft_id`。

**这是整个 Phase 5A 唯一的兼容接缝，故意只留这一个。**

Agent 一路产出的是候选池（c0 到 cN、带评分、带修订次数），而下游的 Guardrail、
Human Review、Review API、Publish 状态机、控制台，认的都是**一条** `draft_content`
加一个 `draft_id`（V1 里由 generate 节点产生）。让下游去理解"候选池"意味着五处
改动加五处新的出错可能；而在这里把池子收敛成一条，下游一行都不用动。

收敛发生在**最后一步**而不是每写一条候选就产生一个 draft，还有第二个理由：
候选池是 Agent 的内部工作状态（写三条、改两条都是过程），把中间稿写进 draft 表
会让人工审核看到一堆从来没人打算发布的版本。
"""

import logging
from datetime import datetime, timezone

from app.agent.budget import counter
from app.agent.schemas import Candidate, CriticResult, DecisionTraceEntry
from app.config import settings
from app.repositories import create_draft, record_decision_result, set_run_topic

logger = logging.getLogger(__name__)

# draft.prompt_version 的取值。与 V1 的 "2" 区分开：事后要能分清一条草稿是
# 老链路写的还是 Agent 写的，否则排查"这条怎么来的"时只能靠猜。
#
# 5b：阅读契约（persona.voice）落地。candidates / revise / critic 三段 prompt
# 与 persona 块都变了，所以已经产出的 5a 草稿与之后的 5b 草稿不是同一套写法，
# 不能混在一张表里比较——那正是这个字段存在的理由。
#
# 5c：治长度遵从。critic 第一次拿到**算好的**字数与硬上限（超上限升 P1），
# revise 拿到"现在多少字、要砍多少"。5b 是"改了声音、没治长度"，两者在长度
# 这个维度上本来就不同，所以必须分开记。
PROMPT_VERSION = "5c"


class AgentIncompleteError(RuntimeError):
    """Agent 结束了却没有产出任何候选正文。

    这**不是**可以静默降级的路径：下游需要一个正文才能进人工审核，
    而"给人工审核一条空内容"比"明确失败"更糟——审核者会以为流程是好的。
    让它以失败收场（run 落 failed + 明确的错误信息）是诚实的做法。
    这条路径同时说明预算或 prompt 需要调整，所以错误信息里带上步数与计数。
    """


def pick_final_candidate(state) -> Candidate:
    """从候选池里选出最终交付的那一条。

    排序规则（按优先级）：
    1. Critic 评价过、且不是 reject 的，分数高的优先；
    2. 没被评价过的（分数未知）次之；
    3. 被 reject 的垫底。

    为什么把"被 reject"排到最后而不是直接丢掉：真到只有 reject 候选可选时，
    交一条明确被否过的稿子并**保留这条否决记录**（它还在 critic_results 里，
    控制台与人工审核看得到），比让整个 Run 失败更有用——人可以直接看到
    "Agent 努力过，但评价说不行"，然后自己判断。

    一个已知的取舍：`revise` 之后若没有重新评价（Planner 选择直接结束），
    被改过的那条带着**改稿前**的分数参与排序。这是可接受的——重新评价在
    正常流程里是允许且更常见的路径（见 budget.allowed_actions 的 writer/critic
    epoch），而按分数取"目前已知最好的那条"在任何时候都不是错的选择。
    """
    candidates: list[Candidate] = list(state.get("candidates") or [])
    if not candidates:  # pragma: no cover - 调用方已先检查
        raise AgentIncompleteError("没有候选可交付")

    verdicts: dict[str, CriticResult] = {
        r.candidate_id: r for r in (state.get("critic_results") or [])
    }

    def rank(candidate: Candidate) -> tuple[int, float]:
        verdict = verdicts.get(candidate.candidate_id)
        if verdict is None:
            return (1, 0.0)
        if verdict.decision == "reject":
            return (2, 0.0)
        return (0, -verdict.score)

    return sorted(candidates, key=rank)[0]


def make_finalize_node(session_factory):
    async def finalize(state) -> dict:
        run_id = state["run_id"]
        candidates = state.get("candidates") or []
        if not candidates:
            raise AgentIncompleteError(
                f"Agent 未产出任何候选正文（步数 {counter(state, 'step_count')}，"
                f"研究 {counter(state, 'research_count')} 次，"
                f"材料 {len(state.get('research_item_ids') or [])} 条）"
            )

        chosen = pick_final_candidate(state)
        step = counter(state, "step_count")
        summary = f"交付候选 {chosen.candidate_id}（{chosen.label}，第 {chosen.revision} 稿，{len(chosen.content)} 字）"

        async with session_factory() as session:
            draft_id = await create_draft(
                session,
                run_id=run_id,
                content=chosen.content,
                version=1,
                prompt_version=PROMPT_VERSION,
                model_name=settings.llm_model,
            )
            # 运行记录列表要显示题目，而这里是**唯一**把它写进 `agent_run.topic`
            # 的地方（工具层不再各写各的）。
            #
            # 题目以 `selected_topic` 为准：它由 `select_topic` 决定，而那个工具
            # 只写 state、不碰库。所以这一句是"Agent 已经选定的题目"落进数据库的
            # 唯一通道——原来写成 `if not state.get("selected_topic")` 时，最正常的
            # 那条路径（select_topic 选完直接出稿）条件恒为假，分支被整个跳过，
            # 库里留下的永远是 NULL。真机数据里 29 个产出了 draft 的 Run 全部如此。
            #
            # 没有 selected_topic 时才回退到**分数最高**的候选：兜底依据必须是
            # trend_score 而不是让模型补一个——这一列会进运行记录列表，也就成了
            # "这次运行在研究什么"的唯一说明，编一个等于让历史记录失真。
            topic = state.get("selected_topic")
            if not topic:
                topics = state.get("candidate_topics") or []
                if topics:
                    topic = max(topics, key=lambda t: t.trend_score).title
            if topic:
                await set_run_topic(session, run_id, topic)

            # finish 那一步没有执行节点（它不进工具表），所以由这里补上结果。
            await record_decision_result(session, run_id=run_id, step=step, result_summary=summary)

        prior = next(
            (e for e in (state.get("decision_trace") or []) if e.step == step), None
        )
        entry = (
            prior
            if prior is not None
            else DecisionTraceEntry(
                step=step,
                action="finish",
                reason="",
                timestamp=datetime.now(timezone.utc),
            )
        ).model_copy(update={"result_summary": summary})

        logger.info("agent finalized (run_id=%s, draft_id=%s)", run_id, draft_id)
        return {
            "draft_content": chosen.content,
            "draft_id": draft_id,
            "decision_trace": [entry],
            "last_result": summary,
        }

    return finalize
