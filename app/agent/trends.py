"""确定性的 trend 打分：一个话题"现在有多值得继续看"。

工作单第九节把话说死了：

> 不要直接让 LLM 随口输出一个分数。分数必须由 deterministic Python 代码计算。

所以本模块是一个**纯函数库**：没有 IO、没有随机、没有 `datetime.now()`
（观察时刻 `observed_at` 一律由调用方传入）。同一份输入必然得到同一个分数，
分数变化必然能追到某个输入字段的变化——这正是"可复现、可测试"的含义。

LLM 的位置在本模块之外：它负责**分组**（哪些帖子在讲同一件事）与**解释**
（为什么值得讨论、还有什么不知道），但它的输出里**没有任何分数字段**
（见 app/agent/schemas.py::TopicClusterDraft）。

## 这个分数不是"X 官方热度排名"

它只是一个**项目内部的排序信号**，由我们在本次扫描里真实取回的数据算出来。
X 的公开 API 只给 like / reply / repost / quote 四个计数，我们把它们记作
**观测到的互动量**（observed engagement），而不是"全网热度"：
我们看不到曝光、看不到转发链、也看不到窗口之外发生了什么。

## 五项口径

| 项 | 量的是什么 | 为什么不是别的 |
| --- | --- | --- |
| recency | 最新一条离现在多近，24 小时半衰期 | "为什么是现在"的量化。没有发布时间就记 0，**不**假装新鲜 |
| spread | 参与讨论的**不同作者数** | 防止"一个大 V 发了一条 → 分数虚高"（工作单第十节） |
| engagement | 观测到的加权互动量，对数饱和 | 转发/引用是**选择放大**，比点赞重；取对数让 500 与 50000 不至于差 100 倍 |
| novelty | 话题在**扫描窗口内**首次出现的位置 | 不是"这个词没见过"：窗口后段才冒出来的才是新事件/新变化 |
| source_quality | 引用里**可核验**的比例 | 编造出处的模型应当被扣分，而不是被静默丢弃后毫无代价 |

### 单一来源上限

只有一条帖子、或只有一位作者时，无论四个计数值多高，`trend_score` 都被压到
`SINGLE_SOURCE_SCORE_CAP` 以下，且**永远不会**被判定为 qualified
（`MIN_AUTHORS_FOR_QUALIFIED` / `MIN_SOURCES_FOR_QUALIFIED`）。
封顶前的分数留在 `trend_score_raw` 里——被压过头而不留痕迹，等于把"为什么只有
40 分"变成一个查不出来的问题。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.agent.schemas import TopicLifecycle, TopicQualification

# --- 撑不起一个选题的材料 ---------------------------------------------------

#: 检索回来的材料里有一类帖子正文**就是一条链接**（分享型）。它没有可以归纳的
#: 内容，标题写出来只会是那条网址，而一条网址会一路往下走：标题 → 选定题目 →
#: 检索词，最后撞在 X 的查询解析器上（URL 含的 `/` 与 `:` 都是非法字符，
#: 见 app/agent/search_query.py），整次运行就此结束。
#:
#: 所以这类材料的标题被替换成下面这句**只陈述事实**的占位文本——它不编造内容，
#: 也不改分数（单一来源本来就会被压到 40 分以下、判成 weak_support）。
LINK_ONLY_TITLE = "（材料只有链接，没有可归纳的正文）"

#: 只由链接组成的文本。
LINK_ONLY_RE = re.compile(r"^(?:https?://\S+\s*)+$")


def is_link_only_title(title: str) -> bool:
    """这个标题是不是一条（或几条）纯链接。

    两处用它，方向相反但理由相同——都因为"网址不是一个话题"：
    `discover` 造候选时把网址换成占位文本，`research` 挑检索词时跳过占位文本
    （占位文本本身也不能当检索词）。
    """
    stripped = (title or "").strip()
    return stripped == LINK_ONLY_TITLE or bool(LINK_ONLY_RE.match(stripped))

# --- 时间口径 ---------------------------------------------------------------

#: 扫描窗口长度：`[observed_at - 72h, observed_at]`。
#: X 的 recent search 本身只覆盖最近 7 天，72 小时是我们主动收窄到"最近三天"
#: （工作单第五节的产品定义就是这个尺度），并且这个窗口会**原样传给 X**
#: 做服务端过滤，而不是取回来之后再本地筛。
TREND_WINDOW_HOURS = 72.0

#: recency 的半衰期。24 小时意味着"昨天的事"折半、"前天的事"剩四分之一。
RECENCY_HALF_LIFE_HOURS = 24.0

#: 超过这个年龄的话题算 stale：不再"值得现在讨论"。
STALE_AFTER_HOURS = 72.0

#: spread 的满值作者数：4 位不同作者即视为"讨论已经扩散开"。
SPREAD_FULL_AUTHORS = 4

#: engagement 的饱和点：加权互动量到这个量级时该项取满分。
#: 取 500 是因为中文科技圈单条帖子的加权互动量通常落在几十到几百之间。
ENGAGEMENT_SATURATION = 500.0

# --- qualified 的门槛 -------------------------------------------------------

#: 分数门槛。低于它的话题仍然会被列出来（Planner 要能看到全部候选），
#: 只是不会被标成 qualified。
QUALIFIED_MIN_SCORE = 55.0

#: 硬性结构门槛：**与分数无关**。分数可以被一条爆款帖子撑起来，
#: 但"只有一条帖子 / 只有一位作者"不是量的问题，是质的问题。
MIN_AUTHORS_FOR_QUALIFIED = 2
MIN_SOURCES_FOR_QUALIFIED = 2

#: 单一来源的分数上限（见模块文档）。
SINGLE_SOURCE_SCORE_CAP = 40.0

#: 五项权重，和为 1。写成元组常量而不是散在公式里：调权重时只改一处，
#: 而"哪一项被调过"在 diff 里一眼可见。
TREND_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("recency", 0.30),
    ("spread", 0.25),
    ("engagement", 0.20),
    ("source_quality", 0.15),
    ("novelty", 0.10),
)

#: 加权互动量里四个计数的权重。
#: 引用（quote）最高：它意味着有人**带着自己的话**把讨论接下去，是"值得讨论"
#: 最直接的形态；转发是纯放大；回复表示有人愿意接话；点赞是最便宜的表态。
ENGAGEMENT_WEIGHTS = {"likes": 1, "reposts": 2, "replies": 2, "quotes": 3}


@dataclass(frozen=True)
class TopicScanItem:
    """参与打分的一条**已观测**帖子。

    刻意不复用 `ResearchResult`：那个模型带正文、URL 等与打分无关的字段，
    而它**没有** `item_id`（库内 id）。把两边塞进一个类型会让"哪个 id 是
    X 的、哪个是本系统的"永远说不清楚——而 `source_post_ids` 指向的必须是
    本系统的 research_item id，否则整条追溯链断在第一步。

    `item_id` 与 `post_id` 的分工：前者是本 Run 内引用材料用的句柄，后者是 X
    的帖子 id，用来**去重**（同一批检索词在不同轮次会取回同一条帖子）。
    """

    item_id: str
    post_id: str
    #: 作者用户名。拿不到时为**空串**——不能拿作者名当"未知作者"的占位符，
    #: 那会让所有未知作者被算成同一个人，直接把 spread 算成 1。
    author: str
    published_at: datetime | None
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0


@dataclass(frozen=True)
class TrendScore:
    """一次打分的全部中间量与结论。字段名与 CandidateTopic 上的一一对应。"""

    discussion_count: int
    author_count: int
    engagement_signal: int
    recency_score: float
    spread_score: float
    novelty_score: float
    source_quality_score: float
    engagement_score: float
    trend_score: float
    trend_score_raw: float
    first_seen_at: datetime | None
    latest_seen_at: datetime | None
    reasons: tuple[str, ...]


def _as_utc(value: datetime) -> datetime:
    """把可能没有时区的 datetime 当作 UTC。

    JSON 里的时间基本都带 Z，但 checkpoint 里的历史数据不一定。
    一个朴素 datetime 直接与 aware datetime 相减会抛 TypeError，
    把一次打分变成一次 500——这里统一口径比在调用方到处防御更可靠。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _published_times(items: list[TopicScanItem]) -> list[datetime]:
    """材料里能确定的发布时间，按时间升序。

    **只收有时间的那些**：缺失时间不是"时间是 1970"，把它当 0 会让一条没带
    时间的帖子把 recency 与 novelty 一起拖到 0——那是编造，不是保守。
    """
    return sorted(_as_utc(i.published_at) for i in items if i.published_at is not None)


def observed_engagement(item: TopicScanItem) -> int:
    """单条帖子的加权互动量（见 ENGAGEMENT_WEIGHTS 的权重理由）。"""
    return (
        item.likes * ENGAGEMENT_WEIGHTS["likes"]
        + item.reposts * ENGAGEMENT_WEIGHTS["reposts"]
        + item.replies * ENGAGEMENT_WEIGHTS["replies"]
        + item.quotes * ENGAGEMENT_WEIGHTS["quotes"]
    )


def total_engagement(items: list[TopicScanItem]) -> int:
    return sum(observed_engagement(i) for i in items)


def distinct_authors(items: list[TopicScanItem]) -> int:
    """不同作者数。空作者名**不计入**——那是"不知道谁发的"，不是一个人。"""
    return len({i.author for i in items if i.author})


def score_recency(items: list[TopicScanItem], observed_at: datetime) -> float:
    """最新一条离观察时刻多近。半衰期 RECENCY_HALF_LIFE_HOURS。

    未来时间（时钟偏差）夹到"刚刚"，而不是给一个大于 1 的分数。
    """
    times = _published_times(items)
    if not times:
        return 0.0
    now = _as_utc(observed_at)
    age_hours = max(0.0, (now - times[-1]).total_seconds() / 3600.0)
    return round(0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS), 4)


def score_spread(items: list[TopicScanItem]) -> float:
    """讨论扩散度：不同作者数 / SPREAD_FULL_AUTHORS，封顶 1。"""
    return round(min(1.0, distinct_authors(items) / SPREAD_FULL_AUTHORS), 4)


def score_engagement(items: list[TopicScanItem]) -> float:
    """观测互动量的对数归一化。

    取 `log1p(总数) / log1p(饱和点)` 而不是线性：互动量分布是长尾的，
    线性归一化会让一条爆款直接吃掉整项，其余话题全挤在 0 附近分不出高下。
    """
    total = total_engagement(items)
    if total <= 0:
        return 0.0
    return round(min(1.0, math.log1p(total) / math.log1p(ENGAGEMENT_SATURATION)), 4)


def score_novelty(
    items: list[TopicScanItem], observed_at: datetime, window_hours: float = TREND_WINDOW_HOURS
) -> float:
    """话题在扫描窗口内的**首次出现位置**。

    0 = 窗口一开始它就在（我们看到的是一段已经在进行的讨论的尾段），
    1 = 窗口快结束时才出现（刚刚冒出来的新事件）。

    这不是"这个词没见过"（工作单第十节明确否掉的）：判断依据是**时间位置**，
    完全来自本次扫描的观测。没有发布时间就记 0——"不知道什么时候开始的"
    不能算成"刚刚开始"。
    """
    times = _published_times(items)
    if not times or window_hours <= 0:
        return 0.0
    now = _as_utc(observed_at)
    window_start = now - timedelta(hours=window_hours)
    span = (now - window_start).total_seconds()
    if span <= 0:  # pragma: no cover - window_hours > 0 时不可达
        return 0.0
    position = (times[0] - window_start).total_seconds() / span
    return round(min(1.0, max(0.0, position)), 4)


def score_source_quality(verified: int, claimed: int) -> float:
    """引用里可核验的比例。

    `claimed` 是模型给出的引用数，`verified` 是其中**真的存在**于本次扫描里的。
    分母用 claimed 而不是 verified 是刻意的：编造出处的模型应当被扣分，
    而不是被静默丢弃后毫无代价（证据工具对事实的处置与这里不同，因为那边
    丢掉一条事实是保护下游，这边丢掉一次扣分是放过上游）。

    没有引用任何出处时记 0：那意味着这个话题**没有任何可追溯的支撑**。
    """
    if claimed <= 0:
        return 0.0
    return round(max(0.0, min(1.0, verified / claimed)), 4)


def _weighted_total(
    *, recency: float, spread: float, engagement: float, novelty: float, source_quality: float
) -> float:
    parts = {
        "recency": recency,
        "spread": spread,
        "engagement": engagement,
        "novelty": novelty,
        "source_quality": source_quality,
    }
    return sum(weight * parts[name] for name, weight in TREND_WEIGHTS) * 100.0


def _explain(
    *,
    items: list[TopicScanItem],
    observed_at: datetime,
    author_count: int,
    engagement: int,
    engagement_score: float,
    recency: float,
    spread: float,
    novelty: float,
    source_quality: float,
    verified: int,
    claimed: int,
    raw: float,
    capped: bool,
) -> tuple[str, ...]:
    """把每个分项写成一句人能核对的话。

    "可解释"不是把公式印出来就完了：人要看的是**这一项为什么是这个数**。
    所以每句话都带上分子分母（几条作者、多少互动、引用几条可核验），
    事后对着原始材料能复核。
    """
    times = _published_times(items)
    reasons: list[str] = []
    if times:
        age_hours = max(0.0, (_as_utc(observed_at) - times[-1]).total_seconds() / 3600.0)
        reasons.append(
            f"最新一条发布于 {age_hours:.1f} 小时前，recency {recency:.4f}"
            f"（半衰期 {RECENCY_HALF_LIFE_HOURS:.0f} 小时）"
        )
    else:
        reasons.append("材料里没有发布时间，recency 记 0")

    reasons.append(
        f"{author_count} 位不同作者参与，spread {spread:.4f}（满值 {SPREAD_FULL_AUTHORS} 人）"
    )
    reasons.append(
        f"观测互动 {engagement}（点赞×1+转发×2+回复×2+引用×3），"
        f"engagement {engagement_score:.4f}（饱和值 {ENGAGEMENT_SATURATION:.0f}）"
    )
    reasons.append(f"窗口内首条出现在 {novelty:.0%} 处，novelty {novelty:.4f}")
    reasons.append(
        f"引用 {claimed} 条来源、其中 {verified} 条可核验，source_quality {source_quality:.4f}"
    )
    formula = " + ".join(f"{name}×{weight:g}" for name, weight in TREND_WEIGHTS)
    reasons.append(f"trend_score_raw = 100 × ({formula}) = {raw:.2f}")
    if capped:
        reasons.append(
            f"单一来源上限 {SINGLE_SOURCE_SCORE_CAP:g} 生效"
            f"（作者数 {author_count} 或可核验来源数 {verified} 不足）"
        )
    return tuple(reasons)


def qualify(
    *,
    score: float,
    author_count: int,
    verified_sources: int,
    latest_seen_at: datetime | None,
    observed_at: datetime,
) -> tuple[TopicQualification, TopicLifecycle]:
    """判"值不值得继续研究"，返回 (qualification, lifecycle)。

    判定顺序是有意的，从**硬约束**到**软约束**：

    1. stale 先判——时间过了就是过了，分数再高也不改变"这是旧闻"这个事实
       （工作单第二十六节明确要求"stale topic 不得被标记为 fresh"）；
    2. 再看支撑结构（作者数 / 可核验来源数）——这是**质**的问题，分数无关；
    3. 最后看分数——这是**量**的问题。

    反过来先看分数的话，一条爆款帖子会把 1、2 两条一起盖过去——而那正是
    工作单第十一节点名要避免的："看到一条热门 Tweet 就直接宣布这是今天热点"。
    """
    if latest_seen_at is None:
        return TopicQualification.STALE, TopicLifecycle.STALE
    age_hours = (_as_utc(observed_at) - _as_utc(latest_seen_at)).total_seconds() / 3600.0
    if age_hours > STALE_AFTER_HOURS:
        return TopicQualification.STALE, TopicLifecycle.STALE

    if author_count < MIN_AUTHORS_FOR_QUALIFIED or verified_sources < MIN_SOURCES_FOR_QUALIFIED:
        return TopicQualification.WEAK_SUPPORT, TopicLifecycle.DISCARDED

    if score < QUALIFIED_MIN_SCORE:
        return TopicQualification.LOW_SIGNAL, TopicLifecycle.DISCOVERED

    return TopicQualification.QUALIFIED, TopicLifecycle.QUALIFIED


def score_topic(
    items: list[TopicScanItem],
    *,
    observed_at: datetime,
    claimed_sources: int,
    window_hours: float = TREND_WINDOW_HOURS,
) -> TrendScore:
    """给一个话题簇打分。本模块唯一的对外入口。

    `items` 是**已经在本次扫描里取回并落库**的帖子，`claimed_sources` 是模型
    声称引用了多少条来源（用来算可核验比例，见 `score_source_quality`）。
    函数不读 state、不读数据库：给它同一份输入，它给同一个分数。
    """
    times = _published_times(items)
    verified = len({i.item_id for i in items})
    author_count = distinct_authors(items)
    engagement = total_engagement(items)

    recency = score_recency(items, observed_at)
    spread = score_spread(items)
    engagement_score = score_engagement(items)
    novelty = score_novelty(items, observed_at, window_hours)
    source_quality = score_source_quality(verified, claimed_sources)

    raw = _weighted_total(
        recency=recency,
        spread=spread,
        engagement=engagement_score,
        novelty=novelty,
        source_quality=source_quality,
    )
    # 单一来源上限：结构上不足以支撑一个"趋势"时，分数不能替它说话。
    capped = (
        author_count < MIN_AUTHORS_FOR_QUALIFIED or verified < MIN_SOURCES_FOR_QUALIFIED
    )
    total = min(raw, SINGLE_SOURCE_SCORE_CAP) if capped else raw

    return TrendScore(
        discussion_count=len(items),
        author_count=author_count,
        engagement_signal=engagement,
        recency_score=recency,
        spread_score=spread,
        novelty_score=novelty,
        source_quality_score=source_quality,
        engagement_score=engagement_score,
        trend_score=round(total, 2),
        trend_score_raw=round(raw, 2),
        first_seen_at=times[0] if times else None,
        latest_seen_at=times[-1] if times else None,
        reasons=_explain(
            items=items,
            observed_at=observed_at,
            author_count=author_count,
            engagement=engagement,
            engagement_score=engagement_score,
            recency=recency,
            spread=spread,
            novelty=novelty,
            source_quality=source_quality,
            verified=verified,
            claimed=claimed_sources,
            raw=raw,
            capped=capped,
        ),
    )


def rank_key(score: TrendScore, title: str) -> tuple:
    """排序键：分数降序，其次讨论条数降序，最后按标题。

    末项用标题而不是"保持原顺序"：LLM 给出的簇顺序对同一批材料并不稳定，
    而只要分数与条数都相同，两者的先后对下游没有意义——用一个确定的字符串
    收尾，至少让同一份输入永远排出同一个顺序。
    """
    return (-score.trend_score, -score.discussion_count, title)
