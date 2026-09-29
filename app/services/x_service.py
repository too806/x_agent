"""X 只读检索的业务封装：原始响应 → `ResearchResult`。

本模块是**来源语义的唯一落点**。三个时间、四个互动计数在这里被明确区分开，
下游（trend 打分、证据、控制台）一律从这个形状读，不再各自解释原始响应。
"""

from datetime import datetime, timezone
from typing import Callable

from app.schemas import ResearchResult
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
            url = f"https://x.com/{username}/status/{post_id}" if username and post_id else None
            results.append(
                ResearchResult(
                    platform_post_id=post_id or "",
                    author_username=username,
                    content=t.get("text", ""),
                    url=url,
                    likes=metrics.get("like_count", 0),
                    reposts=metrics.get("retweet_count", 0),
                    replies=metrics.get("reply_count", 0),
                    # Phase 5B.1：这一项一直就在响应里，之前被丢掉了。
                    quote_count=metrics.get("quote_count", 0),
                    published_at=_parse_iso(t.get("created_at")),
                    observed_at=observed_at,
                )
            )
        return results
