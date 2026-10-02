"""刷新已发布帖子的公开指标（Phase 5B.4）——**手工运行的只读采集脚本**。

    python scripts/refresh_post_metrics.py [--dry-run] [--limit N] [--batch-size N]

## 为什么是脚本，不是调度器

工作单明确禁止本阶段引入 Celery / Redis / Kafka / cron / 分布式调度器。
所以"什么时候抓"由人来决定：跑一次就是一次观察。快照表是只追加的，
重复跑同一次是幂等的（`(x_post_id, observed_at)` 唯一），
所以"多跑几次"没有任何副作用，只是一个更密的观察序列。

## 只读，而且是机器保证的

`RealXApiClient._request` 被换成一层守卫：**非 GET 在发出去之前就抛**
（与 `scripts/smoke_5b1.py` / `collect_style_corpus.py` 同一套做法）。
工作单第三十三节允许 GET、禁止 `POST /2/tweets`；把这句话写成一条运行时检查，
比写在注释里可靠。报告末尾给出按方法分组的请求计数，POST 计数必须是 0。

## 一次抓取 = 一次观察

每个批次共用一个 `observed_at`（由 `XService.fetch_public_metrics` 统一打）。
`hours_since_publish` 也由它算出来，于是"这条快照是发布后多久采的"这件事
在写入的那一刻就被固定下来——事后无法从一个快照行自身重算。
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# 与其它脚本同一个理由：仓库根必须排在 sys.path 最前，否则 `import app`
# 可能静默拿到 editable 安装指向的另一个检出。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import repositories  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services.x_api import MAX_LOOKUP_IDS, RealXApiClient, XApiError  # noqa: E402
from app.services.x_service import XService  # noqa: E402

#: 对 X 发出的每一次请求（方法与路径）。报告末尾据此证明"没有写请求"。
X_REQUESTS: list[dict[str, str]] = []


def install_read_only_guard() -> None:
    """把 X 客户端包成只读的：非 GET 一律在**发出去之前**拒绝。"""
    original = RealXApiClient._request

    async def guarded(self, method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        upper = method.upper()
        params = kwargs.get("params") or {}
        entry: dict[str, str] = {"method": upper, "path": httpx.URL(url).path}
        if params.get("ids"):
            entry["ids"] = str(params["ids"])
        X_REQUESTS.append(entry)
        if upper != "GET":
            raise AssertionError(f"指标刷新只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _hours_between(published_at, observed_at) -> float:
    return (observed_at - published_at).total_seconds() / 3600.0


async def run(args: argparse.Namespace) -> int:
    async with async_session_factory() as session:
        posts = await repositories.list_published_posts_with_x_id(session, limit=args.limit)

    if not posts:
        # 这不是失败：没有已确认发布的帖子时，"没有可采集的东西"就是正确的结论。
        print("没有可采集的帖子：库里没有 status=published 且带 x_post_id / published_at 的行。")
        print("（publishing / publish_unknown 的帖子不进采集——它们的 x_post_id 可能是空的。）")
        return 0

    by_post_id = {post.x_post_id: post for post in posts}
    print(f"发现 {len(posts)} 条已发布帖子，按每批 {args.batch_size} 个 id 抓取公开指标。")

    if args.dry_run:
        for post in posts:
            print(f"  [dry-run] {post.x_post_id}  发布于 {post.published_at.isoformat()}")
        print("dry-run：未发出任何请求，也未写入任何快照。")
        return 0

    written = 0
    skipped = 0
    missing_all: list[str] = []

    async with async_session_factory() as session:
        for batch in _chunks(posts, args.batch_size):
            ids = [post.x_post_id for post in batch]
            service = XService(RealXApiClient())
            observed_at = datetime.now(timezone.utc)
            try:
                observations, not_returned = await service.fetch_public_metrics(
                    ids, observed_at=observed_at
                )
            except XApiError as exc:
                # 这一批**一行都不写**：部分写入会让一份不完整的观察看起来像完整的。
                print(f"抓取失败（批次 {ids[0]}…）：{exc}", file=sys.stderr)
                print("本批次未写入任何行；已写入的批次保持原样。", file=sys.stderr)
                return 1

            missing_all.extend(not_returned)
            for observation in observations:
                post = by_post_id[observation.platform_post_id]
                row = await repositories.insert_post_metrics_snapshot(
                    session,
                    published_post_id=post.id,
                    x_post_id=post.x_post_id,
                    observed_at=observation.observed_at,
                    # 发布时刻与观察时刻都在手上，年龄在写入这一刻算准。
                    hours_since_publish=_hours_between(post.published_at, observation.observed_at),
                    likes=observation.likes,
                    replies=observation.replies,
                    reposts=observation.reposts,
                    quotes=observation.quote_count,
                    # v1 只请求公开计数，这两项恒为 None（不是 0）。
                    impressions=observation.impressions,
                    bookmarks=observation.bookmarks,
                )
                if row is None:
                    skipped += 1
                    print(f"  {post.x_post_id}  此时刻已有快照，跳过（幂等）")
                    continue
                written += 1
                print(
                    f"  {post.x_post_id}  +{row.hours_since_publish:.1f}h  "
                    f"赞 {row.likes}  回复 {row.replies}  转发 {row.reposts}  引用 {row.quotes}"
                )

    print(f"写入 {written} 条快照，跳过 {skipped} 条（幂等重复）。")
    if missing_all:
        # 如实报告"要了但没拿到"的 id：**绝不**为它们补一条全 0 的观察。
        print(f"X 未返回 {len(missing_all)} 个 id（不写入任何行）：{'、'.join(missing_all)}")
    counts: dict[str, int] = {}
    for entry in X_REQUESTS:
        counts[entry["method"]] = counts.get(entry["method"], 0) + 1
    print(f"对 X 的请求：{counts or '无'}（POST 必须为 0）")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="刷新已发布帖子的公开指标（只读）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出将要抓取的帖子，不发请求、不写库"
    )
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理多少条帖子")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=MAX_LOOKUP_IDS,
        help=f"每批多少个 id（X 的上限是 {MAX_LOOKUP_IDS}）",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.batch_size > MAX_LOOKUP_IDS:
        parser.error(f"--batch-size 必须在 1..{MAX_LOOKUP_IDS} 之间")
    return args


def main(argv: list[str]) -> int:
    install_read_only_guard()
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
