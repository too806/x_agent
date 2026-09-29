"""discover_topics / select_topic：先发现一批候选选题，再在候选里选定一个。

## discover_topics 做什么

工作单第七节把它叫 Trend Scout，职责是"从近期 X 中文科技讨论中发现若干候选话题"。
本阶段的实现刻意**不新增数据源**（第六节）：它用现有的 X 只读检索，做三件事——

1. **围出一个有定义的窗口**（最近 `trends.TREND_WINDOW_HOURS` 小时，并把窗口
   原样传给 X 做服务端过滤）。"最近"必须是一个能被写下来的区间，
   否则候选帖子是哪个时期的完全取决于运气。
2. **让模型分组**（哪几条帖子在讲同一件事），这是语言模型擅长且 Python 做不好的部分。
3. **由 Python 打分**（`app/agent/trends.py`）。模型给出的分组里**没有**任何
   分数字段——"有多值得继续看"必须由可复现的算术回答（工作单第九节）。

## 为什么必须聚类，而不是"一条帖子一个话题"

工作单第十一节把这条写成硬要求：不允许"看到一条热门 Tweet 就直接宣布这是今天热点"。
所以走的是 帖子 → 多作者的话题簇 → 打分 的路径，而"单条帖子 / 单一作者"
在打分里被硬性封顶、在判定里被挡在 qualified 之外（见 trends.py）。

## 模型编造出处怎么处理

分组里引用的 material id 会逐个对着本次扫描真实取回的 id 核验：

- **一条都核不上的簇直接丢弃**——那个话题在材料里没有任何落点（与证据工具丢弃
  无出处事实同一套处置）；
- **部分核不上的保留但要扣分**（`trends.score_source_quality` 用"声称数"作分母）。
  两者处置不同是有理由的：丢掉整簇是保护下游，扣分是让上游的编造有代价。
"""

import logging
from datetime import datetime, timedelta

from app.agent.budget import counter
from app.agent.prompts import format_sources, load_prompt, render
from app.agent.schemas import (
    CandidateTopic,
    TopicClusterDraftList,
    TopicLifecycle,
    TopicQualification,
)
from app.agent.tools.base import ToolResult, append_research, complete_json, pairs
from app.agent.trends import TREND_WINDOW_HOURS, TopicScanItem, rank_key, score_topic, qualify
from app.repositories import create_research_items
from app.services.llm import LLMService
from app.services.x_service import XService

logger = logging.getLogger(__name__)

# discovery 要比单主题研究多取一些：它的输入是一个宽泛的目标，
# 而不是一个已经收敛的题目。取 10 条会让候选选题全都长在同一个话题上。
DISCOVERY_RESULTS = 25

# 候选上限。工作单要求"至少产生 5 个候选"，所以下限靠 prompt 约束，
# 这里只做上限保护（防止模型一次吐几十条把后续 prompt 撑爆）。
MAX_TOPICS = 12
MIN_TOPICS = 5

# 兜底线：模型给出的簇里如果连这个数都留不下（全被核验丢掉），
# 就退化成"一条帖子一个候选"。
#
# 为什么要兜底而不是直接报错：一次 discovery 失败会让整条 Run 连一个候选都没有，
# 而失败的原因（模型偶尔不听话）与 Agent 的能力无关。退化出来的候选会被判成
# weak_support、分数封顶在 40 以下——**它不会被误当成好选题**，
# 只是让流程还能往下走，并且这件事会写进 tool summary，藏不住。
FALLBACK_MIN_TOPICS = 2


def _scan_items(item_ids: list[str], results: list) -> list[TopicScanItem]:
    """把"本次取回的材料"整理成打分输入。

    按 X 的 post id 去重，保留**首次**出现的那条：同一次检索里理论上不会重复，
    但窗口重叠的两轮检索会取回同一批帖子，而重复计数会直接把
    `discussion_count` / `author_count` / 互动量一起算高——那是虚高，
    且从结果上看不出来。
    """
    seen: set[str] = set()
    items: list[TopicScanItem] = []
    for item_id, result in zip(item_ids, results):
        post_id = result.platform_post_id or item_id
        if post_id in seen:
            continue
        seen.add(post_id)
        items.append(
            TopicScanItem(
                item_id=item_id,
                post_id=post_id,
                # 拿不到作者名时留空串——不能拿"未知"当作者名，
                # 否则所有未知作者会被算成同一个人，spread 直接变成 1。
                author=result.author_username or "",
                published_at=result.published_at,
                likes=result.likes,
                reposts=result.reposts,
                replies=result.replies,
                quotes=result.quote_count,
            )
        )
    return items


def _material_aliases(item_ids: list[str]) -> dict[str, str]:
    """材料在 prompt 里的短编号（m1、m2…）→ 真实的 `research_item.id`。

    为什么不让模型直接照抄 research_item id：那是一个 36 位的 UUID。一次发现要
    模型在 5～12 个簇里各抄若干条，抄错一位就核验不上。真机第一次跑的结果是
    **7 个簇全部作废**（`kept=0, dropped=7`），整轮发现退化成"一条帖子一个候选"——
    而那正是本阶段最想要的能力，它在界面上和"最近确实没什么可讨论的"长得一样。

    `fact_id` / `angle_id` / `candidate_id` 都已经由工具赋短编号（见
    `app/agent/schemas.py`），材料这里沿用同一套：**短编号由工具定，模型只负责
    逐字照抄**。翻不回来的编号原样保留，于是"编一个不存在的编号"照旧算作
    "声称过但没核验上"，`source_quality` 的扣分逻辑一点都不受影响。
    """
    return {f"m{index}": item_id for index, item_id in enumerate(item_ids, start=1)}


def _apply_aliases(drafts, aliases: dict[str, str]) -> None:
    """把模型给的短编号翻回真 id。就地改，不返回新对象。

    `aliases.get(alias, alias)` 而不是过滤掉认不出的那些：过滤等于把"编了一个
    不存在的编号"变成"没有声称过这条来源"，那会让编造反而免于扣分。
    """
    for draft in drafts:
        draft.source_ids = [aliases.get(source, source) for source in draft.source_ids]


def _fallback_cluster(items: list[TopicScanItem], contents: dict[str, str]) -> list[dict]:
    """确定性兜底：一条帖子一个候选选题。

    标题取正文开头而不是"候选 N"：正文是真实取回的材料，标题是它的引用；
    编一个编号当标题会让人以为那是模型给出的分组结果。
    """
    drafts = []
    for item in items[:MAX_TOPICS]:
        text = (contents.get(item.item_id) or "").strip().replace("\n", " ")
        drafts.append(
            {
                "title": text[:60] or "（无正文的材料）",
                "description": "",
                "keywords": [],
                "why_now": "",
                "unknowns": [],
                "source_ids": [item.item_id],
            }
        )
    return drafts


def _build_topics(drafts, items_by_id: dict, observed_at: datetime) -> tuple[list[CandidateTopic], int]:
    """把模型的分组变成打了分的候选选题。

    返回 (候选列表, 因引用全部核验不上而被丢弃的簇数)。
    """
    known = set(items_by_id)
    scored: list[tuple[CandidateTopic, object]] = []
    dropped = 0

    for draft in drafts[:MAX_TOPICS]:
        # 声称的引用数：**去重后**计数。同一个 id 写两遍不该被算成两条来源，
        # 否则"多写几遍同一个 id"就能把 source_quality 顶到 1。
        claimed_ids = list(dict.fromkeys(_attr(draft, "source_ids") or []))
        verified_ids = [i for i in claimed_ids if i in known]
        if not verified_ids:
            dropped += 1
            continue

        cluster = [items_by_id[i] for i in verified_ids]
        breakdown = score_topic(
            cluster, observed_at=observed_at, claimed_sources=len(claimed_ids)
        )
        qualification, lifecycle = qualify(
            score=breakdown.trend_score,
            author_count=breakdown.author_count,
            verified_sources=len(verified_ids),
            latest_seen_at=breakdown.latest_seen_at,
            observed_at=observed_at,
        )
        topic = CandidateTopic(
            title=_attr(draft, "title"),
            description=_attr(draft, "description"),
            keywords=list(_attr(draft, "keywords") or []),
            source_post_ids=verified_ids,
            first_seen_at=breakdown.first_seen_at,
            latest_seen_at=breakdown.latest_seen_at,
            observed_at=observed_at,
            discussion_count=breakdown.discussion_count,
            author_count=breakdown.author_count,
            engagement_signal=breakdown.engagement_signal,
            recency_score=breakdown.recency_score,
            spread_score=breakdown.spread_score,
            novelty_score=breakdown.novelty_score,
            source_quality_score=breakdown.source_quality_score,
            engagement_score=breakdown.engagement_score,
            trend_score=breakdown.trend_score,
            trend_score_raw=breakdown.trend_score_raw,
            trend_reasons=list(breakdown.reasons),
            why_now=_attr(draft, "why_now"),
            unknowns=list(_attr(draft, "unknowns") or []),
            qualification=qualification,
            lifecycle=lifecycle,
        )
        scored.append((topic, breakdown))

    # 排序后再编号：topic_id 就是名次（t1 是分最高的）。编号在排序前赋的话，
    # "t3 比 t1 分高"会变成常态，而 Planner 的 prompt 里两个字段都在，
    # 一个名次对不上分数的编号比没有编号更误导。
    scored.sort(key=lambda pair: rank_key(pair[1], pair[0].title))
    topics = [
        topic.model_copy(update={"topic_id": f"t{index}"})
        for index, (topic, _) in enumerate(scored, start=1)
    ]
    return topics, dropped


def _attr(draft, name: str):
    """同时接受 pydantic 模型与 dict。

    兜底路径造的是 dict（它不经过 LLM），正常路径是 `TopicClusterDraft`。
    两条路径都走同一个打分/组装函数，是为了保证"兜底出来的候选"与
    "模型给的候选"在字段口径上**没有第二套**。
    """
    return getattr(draft, name) if hasattr(draft, name) else draft.get(name)


def make_discover_tool(x_service: XService, llm_service: LLMService, session_factory):
    async def discover(state, decision) -> ToolResult:
        run_id = state["run_id"]
        goal = state.get("goal") or state.get("topic") or ""

        # 检索词优先取 Planner 给的 arguments.query：把"今天帮我找一个值得讨论的
        # AI 话题"这种自然语言目标直接丢给 X 搜索效果很差——X 只按字面分词匹配，
        # 回来的材料与用户想看的内容没有关系。target 与 goal 是逐级降级的兜底。
        query = str(decision.arguments.get("query") or decision.target or goal)
        if not decision.arguments.get("query"):
            # 降级路径要留痕：检索词退化成整句目标时，回来的候选会很差，
            # 而"候选很差"从结果上看和"最近确实没什么可讨论的"长得一模一样。
            # 一条 warning 至少让这两种情况在日志里分得开。
            logger.warning(
                "agent discover has no search query from planner (run_id=%s), "
                "falling back to %r",
                run_id,
                decision.target or goal,
            )

        # 一次扫描 = 一个观察时刻。窗口的右端、材料的 observed_at、打分用的
        # observed_at 共用它，三处不同就会让 novelty 出现无法解释的偏差。
        observed_at = x_service.now()
        window_start = observed_at - timedelta(hours=TREND_WINDOW_HOURS)

        logger.info(
            "agent discover started (run_id=%s, query=%r, window=%s..%s)",
            run_id,
            query,
            window_start.isoformat(),
            observed_at.isoformat(),
        )
        results = await x_service.research(
            query,
            max_results=DISCOVERY_RESULTS,
            start_time=window_start,
            end_time=observed_at,
            observed_at=observed_at,
        )
        async with session_factory() as session:
            item_ids = await create_research_items(session, run_id, results)

        delta = append_research(state, results, item_ids)

        aliases = _material_aliases(item_ids)
        prompt = render(
            load_prompt("discover"),
            GOAL=goal,
            MIN_TOPICS=MIN_TOPICS,
            MAX_TOPICS=MAX_TOPICS,
            WINDOW_HOURS=int(TREND_WINDOW_HOURS),
            WINDOW_START=window_start.isoformat(),
            # 给模型看的是 m1、m2… 这样的短编号，不是 UUID（见 _material_aliases）。
            RESEARCH=format_sources(
                [(alias, result.content) for alias, result in zip(aliases, results)]
            ),
        )
        parsed: TopicClusterDraftList = await complete_json(
            llm_service, prompt, TopicClusterDraftList
        )
        # 解析之后立刻翻回真 id：往下的核验、打分、`source_post_ids` 全部在
        # 真实 id 空间里进行，短编号不越过这一行。
        _apply_aliases(parsed.topics, aliases)

        items = _scan_items(item_ids, results)
        items_by_id = {item.item_id: item for item in items}
        topics, dropped = _build_topics(parsed.topics, items_by_id, observed_at)

        fallback_note = ""
        if len(topics) < FALLBACK_MIN_TOPICS:
            # 走兜底必须是**响的**：它意味着 prompt 或模型这一轮没给出可用分组，
            # 而这正是下一次调 prompt 时要看的信号。
            kept_from_llm = len(topics)
            logger.warning(
                "agent discover fell back to per-post topics (run_id=%s, kept=%d, dropped=%d)",
                run_id,
                kept_from_llm,
                dropped,
            )
            contents = {item_id: r.content for item_id, r in zip(item_ids, results)}
            topics, _ = _build_topics(
                _fallback_cluster(items, contents), items_by_id, observed_at
            )
            fallback_note = (
                f"（模型只给出 {kept_from_llm} 个可用选题，已退化为按帖子分组）"
            )

        delta["candidate_topics"] = topics
        delta["research_count"] = counter(state, "research_count") + 1

        qualified = sum(1 for t in topics if t.qualification is TopicQualification.QUALIFIED)
        names = "；".join(f"{t.topic_id} {t.title}（{t.trend_score:.1f} 分）" for t in topics[:5]) or "无"
        logger.info("agent discover completed (run_id=%s, topics=%d)", run_id, len(topics))
        summary = (
            f"在最近 {int(TREND_WINDOW_HOURS)} 小时内检索「{query}」取回 {len(results)} 条材料，"
            f"发现 {len(topics)} 个候选选题（其中 {qualified} 个达标）：{names}"
        )
        if dropped:
            summary += f"（丢弃 {dropped} 个引用核验不上的候选）"
        return ToolResult(delta=delta, summary=summary + fallback_note)

    return discover


def make_select_topic_tool():
    """在候选选题里选定一个。

    与 `select_angle` 同一套做法：**不调用 LLM**。分数已经由 `trends.py` 算好并
    排好序，让模型再"选一次"只会给它一次推翻确定性排序的机会，而 trace 里
    记下的"为什么选它"就变成了模型的自述而不是分数。

    target 认两种写法：`topic_id`（t1、t2…）或标题原文。都认不全就回退到
    **分数最高的 qualified 候选**，并在 summary 里写明发生了回退——
    宁可研究一个达标的，也不要研究一个被判定为不值得继续的。
    """

    async def select_topic(state, decision) -> ToolResult:
        topics: list[CandidateTopic] = list(state.get("candidate_topics") or [])
        if not topics:  # pragma: no cover - 前置条件已挡住，防御性分支
            return ToolResult(delta={}, summary="没有可选的话题")

        target = (decision.target or "").strip()
        chosen = next(
            (t for t in topics if t.topic_id == target or t.title == target), None
        )
        how = "按决策选定"
        if chosen is None:
            qualified = [t for t in topics if t.qualification is TopicQualification.QUALIFIED]
            pool = qualified or topics
            chosen = max(pool, key=lambda t: t.trend_score)
            how = (
                f"决策给的「{target or '（空）'}」不是有效候选，"
                f"回退到{'达标的' if qualified else '候选里'}最高分"
            )

        # 生命周期前进一步：被选中意味着接下来会围绕它做研究。
        # 只改命中的那一个，其余保持原样——把整张表重写一遍会让
        # "哪些被看过、哪些没有"这个信息丢掉。
        updated = [
            t.model_copy(update={"lifecycle": TopicLifecycle.RESEARCHING}) if t is chosen else t
            for t in topics
        ]

        logger.info(
            "agent topic selected (run_id=%s, topic=%s)", state["run_id"], chosen.topic_id
        )
        return ToolResult(
            delta={"candidate_topics": updated, "selected_topic": chosen.title},
            summary=(
                f"{how} {chosen.topic_id}：{chosen.title}"
                f"（trend {chosen.trend_score:.1f} 分，{chosen.author_count} 位作者，"
                f"{len(chosen.source_post_ids)} 条来源，判定 {chosen.qualification.value}）"
            ),
        )

    return select_topic
