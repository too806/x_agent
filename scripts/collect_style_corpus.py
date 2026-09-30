"""Style Corpus 的离线采集入口（Phase 5B.2 工作单第九～十四、二十九节）。

```bash
python scripts/collect_style_corpus.py                  # 采集 → 筛选 → 标注 → 算 Profile
python scripts/collect_style_corpus.py --accounts        # 只列出配置里的账号，不发请求
python scripts/collect_style_corpus.py --rebuild-profile # 不采集，只按库里已有样本重算 Profile
python scripts/collect_style_corpus.py --no-annotate     # 采集但不调 LLM 标注（省额度）
python scripts/collect_style_corpus.py --from alice bob  # 只采指定账号
```

## 它为什么是脚本，不是 Agent 工具

工作单第三十六节：style extraction 必须 offline / 预计算，语料与 Agent 运行期
解耦。这里做的一切都发生在**运行之前**——跑完之后 Agent 只读算好的 Profile
（`app/style/runtime.py`）。所以它不在 `max_steps` / `max_research_calls` 里，
不产生 AgentAction，也不受预算约束。

## 只读是怎么被保证的

与 `scripts/smoke_5b1.py` 同一套守卫：`RealXApiClient._request` 被换成一层，
每个请求先落进 `X_REQUESTS` 再放行，**方法不是 GET 就直接抛**。
于是"采集不会发帖"不依赖于"写这段代码的人记得不要发帖"，
而"这次采集对 X 问了什么"留了一份可核对的清单（含每个账号的检索词与窗口）。

## 采集不到东西时

工作单第二十九条：**如实记录实际数量，不伪造、不凑数**。所以脚本：
- 每条被排除的候选都留行并写下理由（`exclusion_reason`），
  于是"为什么这批样本这么少"事后回答得出来；
- 采到 0 条时照常写一份 `sample_count=0` 的 Profile 并明说，
  而不是悄悄跳过——"没有语料"和"语料没算"是两件事。
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# 与 smoke_5b1 同一个理由：仓库根必须排在 sys.path 最前，否则 `import app`
# 可能静默拿到 editable 安装指向的另一个检出。详见该脚本的说明。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402
from app.models import StyleProfile, StyleSample, TrackedAccount  # noqa: E402
from app.services.x_api import RealXApiClient  # noqa: E402
from app.services.x_service import XService  # noqa: E402
from app.style.annotate import (  # noqa: E402
    ANNOTATION_VERSION,
    annotate_sample,
    annotation_timestamp,
)
from app.style.config import load_style_config  # noqa: E402
from app.style.corpus import (  # noqa: E402
    CandidateSample,
    SampleDecision,
    collect_slices,
    collection_query,
    collection_window,
)  # noqa: E402
from app.style.corpus import evaluate_sample  # noqa: E402
from app.style.profile import compute_profile  # noqa: E402
from app.style.schemas import FEATURE_VERSION  # noqa: E402
from app.style.taxonomy import TAXONOMY_VERSION  # noqa: E402

REPORT_PATH = ROOT / "docs" / "phase-5b2-collection.json"

#: 对 X 发出的每一次请求。方法与路径必记；检索请求额外记 query 与窗口——
#: 窗口是"采集用的是 6 天而不是趋势的 72 小时"这条决策**唯一**的执行证据。
X_REQUESTS: list[dict[str, str]] = []


def install_read_only_guard() -> None:
    """把 X 客户端包成只读的：非 GET 一律在**发出去之前**拒绝。"""
    original = RealXApiClient._request

    async def guarded(self, method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        upper = method.upper()
        entry: dict[str, str] = {"method": upper, "path": httpx.URL(url).path}
        if upper == "GET":
            params = kwargs.get("params") or {}
            for key in ("query", "start_time", "end_time", "max_results"):
                if params.get(key) is not None:
                    entry[key] = str(params[key])
        X_REQUESTS.append(entry)
        if upper != "GET":
            raise AssertionError(f"语料采集只允许只读请求，收到了 {upper} {url}")
        return await original(self, method, url, **kwargs)

    RealXApiClient._request = guarded  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


async def sync_accounts(session, accounts) -> dict[str, TrackedAccount]:
    """把配置里的账号同步进 `tracked_account`，返回 username → 行。

    **只增不删。** 从配置里拿掉一个账号不会删掉它的行与历史样本：
    "不再采这个账号"与"把它采过的样本一起抹掉"是两件完全不同的事，
    后者会让已经算出来的 Profile 无法解释。要停采就设 `enabled: false`。
    """
    for account in accounts:
        existing = (
            await session.execute(
                select(TrackedAccount).where(TrackedAccount.username == account.username)
            )
        ).scalars().first()
        if existing is None:
            session.add(
                TrackedAccount(
                    username=account.username,
                    display_name=account.display_name,
                    category=account.category,
                    source_type=account.source_type or "manual",
                    enabled=account.enabled,
                )
            )
        else:
            # 配置是账号元信息的**权威**：人在配置里改了分类，库里要跟上，
            # 否则"按分类统计"用的是改动前的旧值，而且没有任何提示。
            existing.display_name = account.display_name or existing.display_name
            existing.category = account.category or existing.category
            existing.source_type = account.source_type or existing.source_type
            existing.enabled = account.enabled
    await session.commit()

    rows = (await session.execute(select(TrackedAccount))).scalars().all()
    return {row.username: row for row in rows}


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def to_candidate(result) -> CandidateSample:
    """`ResearchResult` → 筛选用的 `CandidateSample`。

    `is_repost` / `is_reply` 直接取映射层判好的值，**不在这里从正文猜**：
    正文以 `@` 开头只是"看起来像回复"，而真正的回复关系只有 X 知道。
    猜出来的标志会让筛选理由变成一句似是而非的话。
    """
    return CandidateSample(
        platform_post_id=result.platform_post_id,
        author_username=result.author_username,
        content=result.content or "",
        published_at=result.published_at,
        source_url=result.url,
        likes=result.likes,
        reposts=result.reposts,
        replies=result.replies,
        quote_count=result.quote_count,
        is_repost=result.is_repost,
        is_reply=result.is_reply,
    )


async def collect_account(x_service, account, config, now: datetime) -> dict:
    """采一个账号。返回这次采集的观察记录（含被排除的与理由）。

    **逐个子窗口问，不是只问一次。** 一次 `/2/tweets/search/recent` 最多回
    100 条且不翻页，只问一次的话"窗口 6 天"实际只覆盖了这个窗口最新的
    一页——而截断不表现在报告里，它看起来就像"这个账号 6 天只发了 100 条"。
    详见 `app/style/corpus.py::collect_slices`。

    跨子窗口按 `platform_post_id` 去重：子窗口在切点处共享端点，落在切点上的
    那条帖子会被两段各取回一次。不去重的话它会带着两个 `observed_at` 竞逐
    同一个唯一键，而"取回了多少条"这个数也会凭空多出来。
    """
    start, end = collection_window(now, config.window_days)
    query = collection_query(account.username)
    slices = collect_slices(start, end, config.slices_per_window)

    results = []
    seen: set[str] = set()
    slice_fetches: list[int] = []
    for slice_start, slice_end in slices:
        batch = await x_service.research(
            query,
            max_results=config.max_results_per_request,
            start_time=slice_start,
            end_time=slice_end,
        )
        slice_fetches.append(len(batch))
        for result in batch:
            post_id = result.platform_post_id
            if post_id and post_id in seen:
                continue
            if post_id:
                seen.add(post_id)
            results.append(result)

    kept: list[tuple[CandidateSample, SampleDecision]] = []
    excluded: list[tuple[CandidateSample, SampleDecision]] = []
    for result in results:
        candidate = to_candidate(result)
        decision = evaluate_sample(candidate, config)
        (kept if decision.included else excluded).append((candidate, decision))

    return {
        "username": account.username,
        "query": query,
        "window": [start.isoformat(), end.isoformat()],
        # 窗口切了几段（= 为这个账号发了几次请求）。与 `window` 一起看，
        # 才能分清"这个账号发得少"和"我们只读了一页"。
        "slices": len(slices),
        # 每一段**实际取回**多少条，以及其中有几段顶到了 `max_results`。
        # 这一段是"窗口到底读完了没有"唯一的依据：某段等于上限就说明那一段
        # 被截断了，报告里"这个账号 6 天只发了这么多"对它不成立——
        # 没有这两个数，"取回 100 条"读起来永远像是账号的真实产出。
        "slice_fetches": slice_fetches,
        "truncated_slices": sum(
            1 for count in slice_fetches if count >= config.max_results_per_request
        ),
        "fetched": len(results),
        "included": len(kept),
        "excluded": len(excluded),
        # 账号自述与显示名：X 的响应里本来就有，回填 `tracked_account`。
        # 取第一条带值的即可——一次 `from:` 检索里的作者必然是同一个账号。
        "author_name": _first_attr(results, "author_name"),
        "author_description": _first_attr(results, "author_description"),
        # 被排除的理由按出现次数汇总。逐条保留在库里，这里只给一个能一眼
        # 看懂的"这批为什么这么少"的回答（工作单第二十九条）。
        "exclusion_reasons": _reason_counts(excluded),
        "_kept": [(c, d) for c, d in kept],
        "_excluded": [(c, d) for c, d in excluded],
        "_observed_at": now,
    }


def _first_attr(results, name: str):
    """第一个非空的属性值。一次 `from:` 检索里的作者必然是同一个账号。"""
    for result in results:
        value = getattr(result, name, None)
        if value:
            return value
    return None


async def backfill_account(session, account_id, collected: dict) -> bool:
    """用 X 给的自述/显示名补上账号元信息。

    **只填空缺，不覆盖已有值**：人写在 `config/style.yaml` 里的名字是权威
    （那可能是他更愿意被怎么称呼），而这里是"X 上是这么写的"。用一个来源
    悄悄盖掉另一个来源，事后没法知道哪个是谁写的。

    参数是 `account_id` 而不是账号行对象：`sync_accounts` 在**另一个**会话里
    读出这些行，那个会话早就关了——拿一个游离（detached）对象改属性再提交，
    新会话对它一无所知，于是 `commit()` 什么也不写，而函数返回 True。
    这正是第一批采集里 9 个账号的 `display_name` 全是空的原因。
    用 UPDATE 语句 + 只填空缺的 `coalesce`，就不存在"对象属于哪个会话"的问题。
    """
    if not collected.get("author_name") and not collected.get("author_description"):
        return False

    statement = (
        update(TrackedAccount)
        .where(TrackedAccount.id == account_id)
        .values(
            # `coalesce` 让"只在为空时写入"成为数据库侧的一个原子判断，
            # 而不是先读后写——先读后写在采集脚本反复跑时会把上一次的值
            # 当成"用户写的"从而永远不更新。
            display_name=func.coalesce(TrackedAccount.display_name, collected.get("author_name")),
            description=func.coalesce(TrackedAccount.description, collected.get("author_description")),
        )
        .returning(TrackedAccount.id)
    )
    result = await session.execute(statement)
    await session.commit()
    return result.scalar_one_or_none() is not None


def _reason_counts(pairs) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, decision in pairs:
        key = decision.reason or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


async def store_samples(session, account_row, collected: dict) -> dict:
    """把一次采集写进 `style_sample`。

    **已存在的 `platform_post_id` 一律跳过，不覆盖。** 重跑采集（换个时间窗口、
    或者只是手滑跑了两次）不该悄悄改掉已有样本的互动数与 `observed_at`——
    那些是"当时观察到的事实"，被后来的数覆盖之后，"这条样本是什么时候看到的"
    就没有答案了。跳过多少条会在报告里如实写着。
    """
    observed_at = collected["_observed_at"]
    inserted = 0
    skipped = 0

    for candidate, decision in collected["_kept"] + collected["_excluded"]:
        if not candidate.platform_post_id:
            continue
        # `platform_post_id` 是 UNIQUE，冲突即已采过。用 ON CONFLICT DO NOTHING
        # 而不是先查后插：先查后插在并发下会撞唯一约束，而这里不需要更复杂的东西。
        statement = (
            pg_insert(StyleSample)
            .values(
                tracked_account_id=account_row.id,
                platform_post_id=candidate.platform_post_id,
                author_username=candidate.author_username,
                content=candidate.content,
                source_url=candidate.source_url,
                # 两个时间都在这里落下：`published_at` 是**来源给的**（原帖什么时候
                # 发的），`observed_at` 是**我们取回它的时刻**。只写后者的话，
                # "这条样本多新"与"这条记录多新"就分不开了（Phase 5B.1 第一节
                # 在 research_item 上已经踩过一次）。
                published_at=candidate.published_at,
                likes=candidate.likes,
                reposts=candidate.reposts,
                replies=candidate.replies,
                quote_count=candidate.quote_count,
                observed_at=observed_at,
                is_repost=candidate.is_repost,
                is_reply=candidate.is_reply,
                is_link_only=decision.reason == "link_only",
                included=decision.included,
                exclusion_reason=decision.reason,
            )
            .on_conflict_do_nothing(index_elements=["platform_post_id"])
            .returning(StyleSample.id)
        )
        result = await session.execute(statement)
        if result.scalar_one_or_none() is None:
            skipped += 1
        else:
            inserted += 1

    await session.commit()
    return {"inserted": inserted, "skipped_existing": skipped}


# ---------------------------------------------------------------------------
# 标注
# ---------------------------------------------------------------------------


async def annotate_pending(session, llm_service, *, limit: int | None = None) -> dict:
    """给"已在语料里但还没标注"的样本补标注。

    只标注 `included=True` 的：被排除的样本不进 Profile，标注它们纯属浪费额度。
    """
    statement = (
        select(StyleSample)
        .where(StyleSample.included.is_(True))
        .where(StyleSample.structure_type.is_(None))
        .order_by(StyleSample.created_at)
    )
    if limit is not None:
        statement = statement.limit(limit)
    rows = (await session.execute(statement)).scalars().all()

    failed: list[str] = []
    for row in rows:
        try:
            annotation = await annotate_sample(llm_service, row.content)
        except Exception as error:  # noqa: BLE001
            # 一条样本标注失败**不该让整批采集失败**：语料是慢慢攒的，
            # 半途炸掉会让"这次到底采了多少"变得没有答案。记下 id 继续。
            failed.append(f"{row.platform_post_id}: {error}")
            continue
        row.content_type = annotation.content_type.value
        row.structure_type = annotation.structure_type.value
        row.hook_type = annotation.hook_type.value
        row.has_claim = annotation.has_claim
        row.has_evidence = annotation.has_evidence
        row.has_counterpoint = annotation.has_counterpoint
        row.has_question = annotation.has_question
        row.has_list = annotation.has_list
        row.has_number = annotation.has_number
        row.has_conclusion = annotation.has_conclusion
        row.annotation_confidence = annotation.confidence
        # 留痕三件套：没有它们，"这个标注是哪版模型什么时候给的"没有答案，
        # 而重跑标注会静默改变 Profile。
        row.annotation_model = getattr(llm_service, "model_name", None) or type(
            llm_service
        ).__name__
        row.annotation_version = ANNOTATION_VERSION
        row.annotated_at = annotation_timestamp()
        await session.commit()

    return {"annotated": len(rows) - len(failed), "failed": failed}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


async def build_profiles(session, *, window: tuple[datetime, datetime]) -> list[dict]:
    """按当前语料重算 Corpus Profile 与各 Account Profile。

    两者**分开存**（工作单第十八节），不合成一个"超级人格"：合起来会让
    "这个特征是谁的"失去答案，而学写法的前提是知道在学谁。
    """
    start, end = window
    computed_at = datetime.now(timezone.utc)
    written: list[dict] = []

    included = (
        await session.execute(select(StyleSample).where(StyleSample.included.is_(True)))
    ).scalars().all()

    corpus_features = compute_profile(included)
    session.add(
        _profile_row(
            corpus_features,
            scope="corpus",
            tracked_account_id=None,
            start=start,
            end=end,
            computed_at=computed_at,
        )
    )
    written.append({"scope": "corpus", "sample_count": corpus_features.sample_count})

    accounts = (await session.execute(select(TrackedAccount))).scalars().all()
    for account in accounts:
        own = [row for row in included if row.tracked_account_id == account.id]
        if not own:
            # 一个账号一条样本都没有时**不写 Profile**：写一行 sample_count=0
            # 的账号档案，看起来像"这个账号写得很平淡"，而事实是"还没采到"。
            continue
        features = compute_profile(own)
        session.add(
            _profile_row(
                features,
                scope="account",
                tracked_account_id=account.id,
                start=start,
                end=end,
                computed_at=computed_at,
            )
        )
        written.append(
            {
                "scope": "account",
                "username": account.username,
                "sample_count": features.sample_count,
            }
        )

    await session.commit()
    return written


def _profile_row(
    features,
    *,
    scope: str,
    tracked_account_id,
    start: datetime,
    end: datetime,
    computed_at: datetime,
) -> StyleProfile:
    """`StyleProfileFeatures` → 表行。逐字段显式搬运，不做魔法映射。

    字段名两边**逐一对齐**（见 `app/models/style_profile.py` 的说明），
    所以漏掉一个列会是显而易见的空白，而不是一个静默的默认值。
    """
    return StyleProfile(
        scope=scope,
        tracked_account_id=tracked_account_id,
        sample_count=features.sample_count,
        sample_window_start=start,
        sample_window_end=end,
        computed_at=computed_at,
        feature_version=FEATURE_VERSION,
        taxonomy_version=TAXONOMY_VERSION,
        median_length=features.median_length,
        p25_length=features.p25_length,
        p75_length=features.p75_length,
        median_paragraphs=features.median_paragraphs,
        mean_sentence_count=features.mean_sentence_count,
        emoji_density=features.emoji_density,
        question_density=features.question_density,
        number_density=features.number_density,
        english_ratio=features.english_ratio,
        structure_distribution=features.structure_distribution,
        hook_distribution=features.hook_distribution,
        opening_distribution=features.opening_distribution,
        closing_distribution=features.closing_distribution,
        sentence_length_distribution=features.sentence_length_distribution,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def run(args) -> int:
    from app.config import settings
    from app.services.llm import OpenAILLMService

    style_config = load_style_config()

    if args.accounts:
        if not style_config.accounts:
            print("config/style.yaml 的 accounts 是空的——采样范围必须由人指定（工作单第七节）。")
            return 1
        for account in style_config.accounts:
            state = "启用" if account.enabled else "停采"
            print(f"  @{account.username}  [{account.category or '未分类'}]  {state}")
        return 0

    now = datetime.now(timezone.utc)
    window = collection_window(now, style_config.corpus.window_days)

    if args.rebuild_profile:
        # **不需要账号清单**：语料已经在库里了，这一步只是按它重算 Profile
        # （改了特征定义、改了分类法之后要跑的就是这个）。把它挂在校验账号
        # 之后会让"我只想重算一下"变成一句"accounts 是空的"。
        report = {
            "phase": "5B.2",
            "generated_at": now.isoformat(),
            "note": "只重算 Profile，没有采集（不产生任何 X 请求）。",
            "window_days": style_config.corpus.window_days,
            "window": [window[0].isoformat(), window[1].isoformat()],
            "accounts": [],
        }
        async with async_session_factory() as session:
            report["profiles"] = await build_profiles(session, window=window)
        report["x_requests"] = {"total": 0, "by_method": {}, "queries": []}
        _write_report(report)
        for entry in report["profiles"]:
            print(f"  {entry}")
        print(f"\n记录已写入 {REPORT_PATH}")
        return 0

    if not style_config.accounts:
        print(
            "config/style.yaml 的 accounts 是空的。\n"
            "采集范围**必须由人指定**（工作单第七、三十节）："
            "这里不提供自动发现，也不会替你挑账号。\n"
            "（若只是想按库里已有的样本重算 Profile，用 --rebuild-profile。）"
        )
        return 2

    selected = set(args.from_accounts or [])
    accounts = [
        a
        for a in style_config.accounts
        if a.enabled and (not selected or a.username in selected)
    ]
    if selected:
        missing = selected - {a.username for a in accounts}
        if missing:
            print(f"配置里没有这些账号（或它们被停采了）：{sorted(missing)}")
            return 2
    if not accounts:
        print("没有启用的账号可采。")
        return 0

    now = datetime.now(timezone.utc)
    window = collection_window(now, style_config.corpus.window_days)
    report: dict = {
        "phase": "5B.2",
        "generated_at": now.isoformat(),
        "note": (
            "Style Corpus 的一次采集记录。语料是**真实 X 只读检索**的产物，"
            "因此不可复现：它不是基准，是当时的记录（工作单第二十九节）。"
        ),
        "window_days": style_config.corpus.window_days,
        "window": [window[0].isoformat(), window[1].isoformat()],
        "accounts": [],
    }

    llm_service = OpenAILLMService()
    x_service = XService(RealXApiClient())

    async with async_session_factory() as session:
        account_rows = await sync_accounts(session, accounts)

    for account in accounts:
        print(f"\n>>> 采集 @{account.username} …", flush=True)
        collected = await collect_account(x_service, account, style_config.corpus, now)
        async with async_session_factory() as session:
            account_row = account_rows[account.username]
            await backfill_account(session, account_row.id, collected)
            stored = await store_samples(session, account_row, collected)
        # `author_description` 刻意**不进报告**。它是账号自述，而自述里常有
        # 邮箱、微信号、联系方式这类个人信息——这份报告是要提交进仓库的。
        # 回填数据库需要它（那是本地数据），留下可核对的采集记录不需要它：
        # 账号名与显示名已经足够回答"这批语料学了谁"。
        entry = {
            k: v
            for k, v in collected.items()
            if not k.startswith("_") and k != "author_description"
        }
        entry.update(stored)
        report["accounts"].append(entry)
        print(
            f"    取回 {entry['fetched']} 条，纳入 {entry['included']} 条，"
            f"新入库 {stored['inserted']} 条，已存在跳过 {stored['skipped_existing']} 条",
            flush=True,
        )

    if args.annotate:
        async with async_session_factory() as session:
            annotation = await annotate_pending(
                session, llm_service, limit=args.annotate_limit
            )
        report["annotation"] = annotation
        print(
            f"\n标注完成：{annotation['annotated']} 条"
            + (f"，失败 {len(annotation['failed'])} 条" if annotation["failed"] else ""),
            flush=True,
        )

    async with async_session_factory() as session:
        report["profiles"] = await build_profiles(session, window=window)

    report["x_requests"] = {
        "total": len(X_REQUESTS),
        "by_method": _method_counts(),
        # 供人一眼确认：整份采集记录里出现过的检索词。
        "queries": sorted({r["query"] for r in X_REQUESTS if "query" in r}),
    }

    _write_report(report)
    print(f"\n记录已写入 {REPORT_PATH}")
    print(f"对 X 发出的请求：{len(X_REQUESTS)} 次，全部为 GET。")
    return 0


def _write_report(report: dict) -> None:
    """把一份记录**追加**进报告文件的 `runs`。

    语料是**累积**的：一份新报告盖掉旧的，等于丢掉"上次采到多少、哪几条被
    排除了"——而那正是下次调阈值时要看的东西。
    """
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    document: dict = {}
    if REPORT_PATH.exists():
        try:
            loaded = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
        except json.JSONDecodeError:
            # 报告文件被手改坏时**不要**阻止采集完成：这时候语料已经进库了，
            # 抛异常会让人以为采集失败。从空文档重新开始，并留下一句说明。
            document = {"note": "上一份报告无法解析，已从本次开始重建。"}
    document.setdefault("phase", "5B.2")
    document.setdefault("note", "每次采集一份记录，累积保存在 runs 里。")
    document.setdefault("runs", []).append(report)
    REPORT_PATH.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _method_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in X_REQUESTS:
        counts[entry["method"]] = counts.get(entry["method"], 0) + 1
    return counts


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Style Corpus 采集（只读）")
    parser.add_argument(
        "--accounts", action="store_true", help="只列出配置里的账号，不发任何请求"
    )
    parser.add_argument(
        "--rebuild-profile",
        action="store_true",
        help="不采集，只按库里已有的样本重算 Profile",
    )
    parser.add_argument(
        "--no-annotate", dest="annotate", action="store_false", default=True,
        help="采集但不调 LLM 标注",
    )
    parser.add_argument(
        "--annotate-limit", type=int, default=None, help="本次最多标注多少条"
    )
    parser.add_argument(
        "--from", dest="from_accounts", nargs="*", default=None,
        help="只采这些账号（用户名，不带 @）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    install_read_only_guard()
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
