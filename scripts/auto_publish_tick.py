"""跑一轮无人值守自动发布（**主机制**，配 Windows 计划任务）。

    python scripts/auto_publish_tick.py [--json] [--status]

## 为什么是 CLI + 计划任务，不是进程内调度器

放在 FastAPI 的 lifespan 里技术上完全可行（`run.py` 无 `--reload` 无 `--workers`，
单进程），但一旦有人加上 `--reload` 或 `--workers N`，N 个调度器会各自建 Run、
各自可能发布——而**重复发布是这套系统能产生的最坏结果**，且从进程内部无法可靠地
检测 reload。另外这台机器上后端是一个 cmd 窗口，关掉就停，
这与"无人值守"正相反；关停时若 cancel 落在 `_call_x` 中间，
正好落进需要人工裁决的 `publish_unknown` 窗口。

仓库自己的姿态也一样：`scripts/refresh_post_metrics.py` 记录了工单"明确禁止
引入分布式调度器"。机制保持最小、外部、无常驻。

计划任务示例（每 30 分钟一次）：

    schtasks /create /tn "XAgent auto publish" /sc minute /mo 30 ^
      /tr "cmd /c cd /d C:\\xuexi\\py\\2026_agent\\X_Agent && python scripts\\auto_publish_tick.py"

## 影子模式下这是硬保证，不是约定

`mode: shadow` 时本脚本把 `RealXApiClient._request` 包成**只读**的：
非 GET 在发出去之前就抛（与 `scripts/smoke_5b1.py` / `refresh_post_metrics.py`
同一套做法）。于是"影子模式不发布"这件事在这个进程里是**运行时会失败的**，
而不是"代码里看起来没有那条路径"——后者在下一次重构时就不成立了。

## 退出码

    0 = 这一轮跑完了（包括 disabled / locked / run_cap 这些"正常地什么都不做"）
    1 = 这一轮出了问题（配置读不了 / Agent 抛异常 / 超时）

刻意把"什么都没做"算成功：调度器下一次还会来，而 disabled 是一个
**人在配置里明确表达的状态**，不是故障。把它算成失败只会让事件日志里
每 30 分钟多一条红色的、需要被忽略的记录。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 与其它脚本同一个理由：仓库根必须排在 sys.path 最前，否则 `import app`
# 可能静默拿到 editable 安装指向的另一个检出。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.checkpointer import create_checkpointer  # noqa: E402
from app.main import build_runtime  # noqa: E402
from app.services.auto_publish import (  # noqa: E402
    OUTCOME_CONFIG_ERROR,
    OUTCOME_ERROR,
    OUTCOME_NO_DRAFT,
    OUTCOME_TIMEOUT,
    AutoPublishService,
    collect_status,
)
from app.services.automation_config import load_automation_config  # noqa: E402
from app.services.x_api import RealXApiClient  # noqa: E402

#: 对 X 发出的每一次请求（方法与路径）。影子模式下用来证明"没有写请求"。
X_REQUESTS: list[dict[str, str]] = []

#: 这一轮真正会出问题的结论。
FAILING_OUTCOMES = frozenset(
    {OUTCOME_CONFIG_ERROR, OUTCOME_ERROR, OUTCOME_TIMEOUT, OUTCOME_NO_DRAFT}
)


def install_shadow_read_only_guard() -> None:
    """把 X 客户端包成只读的：非 GET 一律在**发出去之前**拒绝。

    只在 `mode: shadow` 时安装。这个守卫的代价是它在发布路径上引入了一个
    新的失败点——所以正式模式（那本来就要发帖）下不装。
    """
    original = RealXApiClient._request

    async def guarded(self, method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        upper = method.upper()
        X_REQUESTS.append({"method": upper, "path": httpx.URL(url).path})
        if upper != "GET":
            raise AssertionError(
                f"影子模式不允许任何写请求，收到了 {upper} {httpx.URL(url).path}"
            )
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


def _print_status(payload) -> None:
    a = payload.agreement
    rate = "—" if a.agreement_rate is None else f"{a.agreement_rate:.0%}"
    print(f"配置：enabled={payload.enabled}  mode={payload.mode}")
    if payload.config_error:
        print(f"  配置读不了：{payload.config_error}")
    print(
        f"一致率：{rate}（已决 {a.decided_pairs} 对；"
        f"误放行 {a.false_approve}，改稿 {a.soft_mismatch}，误拦 {a.false_reject}）"
    )
    print(f"转正式门槛：{'全部自动判据通过' if payload.gate.ready else '未通过'}")
    for c in payload.gate.criteria:
        print(f"  [{'x' if c.met else ' '}] {c.label} —— {c.detail}")
    print(f"仍需人工完成的 {len(payload.gate.manual_steps)} 步：")
    for step in payload.gate.manual_steps:
        print(f"  - {step}")


async def _run_tick(args: argparse.Namespace) -> int:
    url = settings.langgraph_checkpoint_database_url or settings.database_url
    async with create_checkpointer(url) as checkpointer:
        runtime = build_runtime(checkpointer)

        if args.status:
            payload = await collect_status(runtime.session_factory)
            if args.json:
                print(json.dumps(payload.model_dump(), ensure_ascii=False, indent=2))
            else:
                _print_status(payload)
            return 0

        # 先读一次配置来决定要不要装只读守卫。调用方（tick）会再读一次，
        # 那次读到的才是生效的——这里读到的只用来选守卫，中间被改掉的话，
        # 守卫方向可能偏保守（影子被当成正式），不会偏危险。
        try:
            shadow = load_automation_config().is_shadow
        except Exception:  # noqa: BLE001
            # 配置坏了：tick 自己会报 config_error。这里默认**按影子装守卫**，
            # 方向是保守的——多装一次守卫只是让一次写请求失败，
            # 少装一次则是在不知道配置是什么的情况下把写请求放出去。
            shadow = True

        if shadow and not args.no_guard:
            install_shadow_read_only_guard()

        result = await AutoPublishService(runtime).tick()

        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"tick：{result.outcome}  mode={result.mode}")
            if result.run_id:
                print(f"  run_id={result.run_id}  topic={result.topic!r}")
            if result.verdict:
                print(
                    f"  判据：{result.verdict}"
                    f"（approved={result.approved}  published={result.published}）"
                )
            for reason in result.reasons:
                print(f"    · {reason}")
            if result.reaped:
                print(f"  回收残留 claim：{result.reaped}")
            if result.error:
                print(f"  错误：{result.error}", file=sys.stderr)
            if shadow:
                counts: dict[str, int] = {}
                for entry in X_REQUESTS:
                    counts[entry["method"]] = counts.get(entry["method"], 0) + 1
                print(f"  对 X 的请求：{counts or '无'}（影子模式下 POST 必须为 0）")

        return 1 if result.outcome in FAILING_OUTCOMES else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跑一轮无人值守自动发布")
    parser.add_argument(
        "--status",
        action="store_true",
        help="只打印一致率与转正式门槛，不跑 tick、不改任何东西",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是人读的文本")
    parser.add_argument(
        "--no-guard",
        action="store_true",
        help="影子模式下也不装只读守卫（仅供排障，正常不要用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    return asyncio.run(_run_tick(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
