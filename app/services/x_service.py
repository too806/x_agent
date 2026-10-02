"""X 只读检索的业务封装：原始响应 → `ResearchResult`。

本模块是**来源语义的唯一落点**。三个时间、四个互动计数在这里被明确区分开，
下游（trend 打分、证据、控制台）一律从这个形状读，不再各自解释原始响应。
"""

from datetime import datetime, timezone
from typing import Callable

from app.schemas import PostMetricsObservation, ResearchResult
from app.services.x_api import XApiClient


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(text: str | None) -> datetime | None:
    """X 的 RFC3339 时间串 → datetime。

    解析不出来就返回 None，**不猜一个时间**：一个编造的发布时间会让一条旧讨论
    在 trend 里显得刚刚发生，而这种错误在报告里看不出来。
    """
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class XService:
    """面向业务的研究封装：调用 XApiClient，把原始响应映射为 ResearchResult。

    `clock` 可注入是为了测试：`observed_at` 必须能被固定，否则任何依赖它的
    断言都只能在"跑得够快"的假设下成立。生产路径用 UTC now。
    """

    def __init__(self, client: XApiClient, clock: Callable[[], datetime] | None = None):
        self._client = client
        self._clock = clock or _utcnow

    def now(self) -> datetime:
        """当前观察时刻。

        工具用它算扫描窗口，而不是各自去调 `datetime.now()`：一次扫描的窗口起点、
        终点与整批材料的 `observed_at` 必须是**同一个瞬间**，否则 novelty 这类
        "在窗口里排第几"的量会因为几次取时间之间的漂移而变。也让测试能把整个
        窗口钉死。
        """
        return self._clock()

    async def research(
        self,
        topic: str,
        max_results: int = 10,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        observed_at: datetime | None = None,
    ) -> list[ResearchResult]:
        """一次只读检索。

        `start_time` / `end_time` 是**扫描窗口**：调用方必须说清自己在看哪一段时间，
        否则"最近"就是一个没有定义的概念（Phase 5B.1 第三节）。两者都传给 X，
        由 X 侧过滤，而不是拿回一批再在本地丢——本地丢会让 `max_results`
        被窗口外的帖子占满，实际拿到的新材料可能一条都没有。

        `observed_at` 由调用方传入时整批共用同一个值：**一次检索就是一次观察**。
        不传则由 clock 取一次，同样整批共用一个值。
        """
        raw = await self._client.search_recent_tweets(
            topic, max_results, start_time=start_time, end_time=end_time
        )
        return self._map(raw, observed_at=observed_at or self._clock())

    async def fetch_public_metrics(
        self,
        ids: list[str],
        *,
        observed_at: datetime | None = None,
    ) -> tuple[list[PostMetricsObservation], list[str]]:
        """按 id 读我们自己已发布帖子的公开指标（只读，Phase 5B.4）。

        返回 `(observations, ids_not_returned)`。

        ## 为什么"没被返回"要单独交出去

        X 的 `/2/tweets?ids=` 对读不到的 id（删除了、被限流、权限不够）**直接
        不放进 `data`**，同时可能在 `errors` 里给一条。两种情况下我们手上都
        没有计数——这时唯一的正确做法是把它列进 `ids_not_returned` 让调用方
        如实报告，而不是补一条全 0 的观察。补 0 会把"读不到"记成"没人互动"，
        而这两件事在反馈里含义相反。

        ## 为什么缺字段是 None 而不是 0

        与 `_map` 不同（那一路径的既定口径是补 0）。这里逐个键用 `.get(key)`
        取默认 `None`：X 没有给 `like_count` 时，我们**没有**观测到 0 个赞。
        一次抓取一个 `observed_at`，整批共用（同 `research`）。
        """
        if not ids:
            return [], []
        raw = await self._client.get_posts_by_ids(list(ids))
        stamp = observed_at or self._clock()

        observations: list[PostMetricsObservation] = []
        returned: set[str] = set()
        for entry in raw.get("data") or []:
            post_id = entry.get("id")
            if not post_id:
                continue
            post_id = str(post_id)
            returned.add(post_id)
            metrics = entry.get("public_metrics") or {}
            observations.append(
                PostMetricsObservation(
                    platform_post_id=post_id,
                    # 逐个键取，默认 None——**不要**抄 `_map` 里的
                    # `metrics.get("like_count", 0)`：那是"别人帖子"的口径。
                    likes=metrics.get("like_count"),
                    replies=metrics.get("reply_count"),
                    reposts=metrics.get("retweet_count"),
                    quote_count=metrics.get("quote_count"),
                    # v1：这两项从未请求，恒为 None（字段留在 schema 里而已）。
                    impressions=metrics.get("impression_count"),
                    bookmarks=metrics.get("bookmark_count"),
                    observed_at=stamp,
                )
            )
        not_returned = [pid for pid in ids if pid not in returned]
        return observations, not_returned

    async def publish(self, content: str) -> str:
        """发布推文，返回 X 侧 post id。

        刻意不做任何异常转换：发布路径需要区分"确定没发出去"（4xx / 连接失败）
        和"结果未知"（超时 / 5xx），这个区分必须在状态机里做，而且依赖
        x_api 抛出的具体类型与 status_code，包一层只会把信息磨掉。
        """
        return await self._client.create_post(content)

    def _map(self, raw: dict, *, observed_at: datetime) -> list[ResearchResult]:
        data = raw.get("data") or []
        users = {u["id"]: u for u in (raw.get("includes") or {}).get("users", [])}
        results: list[ResearchResult] = []
        for t in data:
            author = users.get(t.get("author_id"))
            username = author.get("username") if author else None
            metrics = t.get("public_metrics") or {}
            post_id = t.get("id")
            referenced = [
                entry.get("type")
                for entry in (t.get("referenced_tweets") or [])
                if isinstance(entry, dict)
            ]
            url = f"https://x.com/{username}/status/{post_id}" if username and post_id else None
            results.append(
                ResearchResult(
                    platform_post_id=post_id or "",
                    author_username=username,
                    # 显示名与自述：回填 tracked_account 用。缺失时是 None，
                    # 不编一个——账号元信息是"记录下来的"，不是"补出来的"。
                    author_name=author.get("name") if author else None,
                    author_description=author.get("description") if author else None,
                    content=t.get("text", ""),
                    url=url,
                    likes=metrics.get("like_count", 0),
                    reposts=metrics.get("retweet_count", 0),
                    replies=metrics.get("reply_count", 0),
                    # Phase 5B.1：这一项一直就在响应里，之前被丢掉了。
                    quote_count=metrics.get("quote_count", 0),
                    published_at=_parse_iso(t.get("created_at")),
                    observed_at=observed_at,
                    # Phase 5B.2：`referenced_tweets` 非空说明这条指向了别的
                    # 帖子（转发 / 引用 / 回复）。这个判断**事后从正文看不出来**，
                    # 所以必须在映射时就记下来（corpus 采集据此剔除转发）。
                    #
                    # 两个标志必须分开：`referenced_tweets` 是一个**混装**列表，
                    # 只看"非空"会把一条回复标成转发，于是筛选层写下一个
                    # **错误的理由**。引用仍然算 repost（它的正文确实不是
                    # 自己的完整表达），回复单独一档。
                    is_repost=any(kind in ("retweeted", "quoted") for kind in referenced),
                    is_reply="replied_to" in referenced,
                )
            )
        return results
