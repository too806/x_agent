"""Style Corpus 的采集与筛选（Phase 5B.2 工作单第九～十二节）。

## 采集是 offline 的

工作单第三十六节：style extraction 应当 offline / 预计算，语料必须与 Agent
运行期解耦。所以本模块**不在 Agent 运行期被调用**——它由采集脚本（`scripts/`）
驱动，跑完把语料与 Profile 写进库，运行期只读算好的 Profile。

## 筛选是**组合**条件，阈值全在配置里

工作单第十一、十二节。这里把规则写成一个纯函数 `evaluate_sample()`：
给定一条候选与配置，返回"收不收 + 为什么"。拆成纯函数有两个好处：

- 可以单独测每一条规则（不需要数据库，也不需要 X）；
- 被排除的样本**仍然留行并记下理由**——否则"为什么这批样本这么少"
  永远无法回答，而采集规模正是要如实记录的东西（工作单第二十九条）。

## 互动量不是质量真值

工作单第十二节把这条写成硬要求。这里的互动门槛只做一件事：把明显的零互动
灌水挡在外面。它在 Profile 里**不参与任何质量打分**——互动高不等于写法值得学。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from app.style.config import CorpusConfig

#: 只含 URL 的正文。用来判 `is_link_only`：去掉链接后没有实质内容。
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

#: 短帖里的广告/抽奖标记。它们与内容质量无关，只是不该进语料。
_PROMO_MARKERS: tuple[str, ...] = ("抽奖", "转发本条", "giveaway", "关注并转发", "优惠码")


@dataclass(frozen=True)
class SampleDecision:
    """一条候选样本的筛选结论。

    `included` 为 False 时 `reason` 一定非空——被排除的样本必须留下理由，
    否则语料规模不足时无从解释。反过来，`included` 为 True 时 `reason` 是
    `None`：没有理由要写，编一个"因为合格"只会让日志多出一行噪声。
    """

    included: bool
    reason: str | None = None


@dataclass(frozen=True)
class CandidateSample:
    """筛选用的一条候选。字段与 `ResearchResult` 对齐，但**不依赖它**——
    这一层只关心"够不够格进语料"，不该绑定采集侧的模型形状。

    `published_at` / `source_url` 是**筛选用不到**的字段，在这里是因为
    采集脚本靠这个对象把一条候选一路带到落库那一步：没有它们，来源侧
    那两个"事后补不回来"的事实（原帖什么时候发的、原文在哪里）就在
    从映射到写库的路上被丢掉了，而丢掉之后没有任何东西会报错——
    只是每一行样本的 `published_at` 都是空的。
    """

    platform_post_id: str
    author_username: str | None
    content: str
    #: 原帖在 X 上的发布时间。**来源给的时间**，缺失就是 `None`。
    published_at: datetime | None = None
    source_url: str | None = None
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quote_count: int = 0
    is_repost: bool = False
    #: 是不是回复别人的帖子。与 `is_repost` 分开（见 `app/schemas.py` 的说明）：
    #: X 的 `referenced_tweets` 混装三种关系，只看"非空"会让一条回复
    #: 带着一个错误理由被剔掉。
    is_reply: bool = False


def engagement_score(candidate: CandidateSample, weights: dict[str, int]) -> int:
    """四项互动按权重折算成一个标量。**只用于过门槛**，不进 Profile。"""
    return (
        candidate.likes * weights.get("likes", 0)
        + candidate.reposts * weights.get("reposts", 0)
        + candidate.replies * weights.get("replies", 0)
        + candidate.quote_count * weights.get("quotes", 0)
    )


def is_link_only(content: str) -> bool:
    """正文**只有**一条链接，没有可学的写法。

    先要求真的存在一条链接：没有链接的帖子谈不上"只有链接"，它只是短。
    少了这一步，一条三五个字的短帖会因为"去掉链接后不足 10 字"被判成
    link_only，而它压根没有链接——于是 `evaluate_sample` 的短路理由
    变成了一句误导人的话，而"理由总是最具体的那一条"正是短路存在的目的。

    阈值取 10 个字符：`看这个 https://...` 去掉链接只剩"看这个"，
    学不到写法。10 是"还剩下半句话"与"什么都不剩"之间的一条线。
    """
    text = content or ""
    if not _URL.search(text):
        return False
    stripped = _URL.sub("", text).strip()
    return len(stripped) < 10


def evaluate_sample(
    candidate: CandidateSample, config: CorpusConfig | None = None
) -> SampleDecision:
    """一条候选进不进 corpus。**组合条件**，短路在第一处不满足的规则。

    短路顺序是刻意的：先判最便宜、最确定的（长度、纯链接），再判需要计算的
    （互动折算）。这样被排除的理由总是**最具体**的那一条，而不是"综合不合格"。
    """
    cfg = config or CorpusConfig()
    content = candidate.content or ""

    # 1. 正文完整性。空正文没有任何可学的结构。
    if not content.strip():
        return SampleDecision(False, "empty_content")
    # 2. 纯链接。去掉链接后没有实质内容，学不到写法。
    if cfg.exclude_link_only and is_link_only(content):
        return SampleDecision(False, "link_only")
    # 3. 转发/引用。要学的是"他怎么写自己的话"，不是"他转了什么"。
    if cfg.exclude_reposts and candidate.is_repost:
        return SampleDecision(False, "repost")
    # 4. 回复别人的帖子。**与转发分开判**：回复是对着别人的半句话接话，
    #    没有自己的开头与收尾。把它混进语料会让段落数、开头分布与结尾分布
    #    一起失真，而 Profile 正是从这三样里读出"一段完整表达长什么样"的。
    if cfg.exclude_replies and candidate.is_reply:
        return SampleDecision(False, "reply")
    # 4. 长度合法区间。太短的学不到结构，太长的多半是长文串。
    if len(content) < cfg.min_length:
        return SampleDecision(False, f"too_short(<{cfg.min_length})")
    if len(content) > cfg.max_length:
        return SampleDecision(False, f"too_long(>{cfg.max_length})")
    # 5. 广告/抽奖。与写法无关。
    for marker in _PROMO_MARKERS:
        if marker in content:
            return SampleDecision(False, f"promo({marker})")
    # 6. 互动门槛。唯一的粗筛，且**不代表质量**（工作单第十二节）。
    if candidate.likes < cfg.min_likes:
        return SampleDecision(False, f"low_likes(<{cfg.min_likes})")
    score = engagement_score(candidate, cfg.engagement_weights)
    if score < cfg.min_engagement_score:
        return SampleDecision(False, f"low_engagement(<{cfg.min_engagement_score})")

    return SampleDecision(True, None)


def filter_candidates(
    candidates: Iterable[CandidateSample], config: CorpusConfig | None = None
) -> list[tuple[CandidateSample, SampleDecision]]:
    """批量筛选，**保留被排除的**（连同理由）。

    不在这里丢掉被排除项：调用方要写库的正是"收进来的 + 没收进来的及原因"
    两部分。返回全部结论，让丢不丢由调用方决定。
    """
    cfg = config or CorpusConfig()
    return [(candidate, evaluate_sample(candidate, cfg)) for candidate in candidates]


# ---------------------------------------------------------------------------
# 采集窗口
# ---------------------------------------------------------------------------


def collection_query(username: str) -> str:
    """采样查询词。`from:<username>` 只取这个账号自己发的帖子。"""
    return f"from:{username.lstrip('@')}"


def collection_window(now: datetime, window_days: int) -> tuple[datetime, datetime]:
    """`[now - window_days, now]`。

    与 trend 的 72 小时窗口**分开**（工作单决策 A）：72 小时回答"为什么这条
    讨论是现在值得写的"，这个回答"这些人平时怎么写"。两个口径混在一起会让
    两边都失去意义。

    这里是纯函数（传入 `now`），因为它必须可测——窗口端点直接决定能采到
    哪些帖子，而"跑的时候是几点"不该影响测试结果。
    """
    return now - timedelta(days=window_days), now


def collect_slices(
    window_start: datetime, window_end: datetime, slices: int
) -> list[tuple[datetime, datetime]]:
    """把一个采集窗口切成若干**首尾相接、互不重叠**的子窗口。

    ## 为什么必须切

    `/2/tweets/search/recent` 一次最多回 100 条，而且**这个客户端不做翻页**
    （不带 `next_token`）。所以"6 天窗口"在一次请求里实际只能看到这个窗口
    **最新的那 100 条**——而实测里这些账号的帖子有 89% 是回复与转发，
    两类都要被筛掉，于是 100 条里往往只剩个位数。

    截断发生在一个看不见的地方：报告上写着"窗口 6 天、取回 100 条"，
    读起来像是"这个账号 6 天只发了 100 条"，而事实是"我们只看了最新的一页"。
    这个区别骗不了别人，只会骗到下一个人。

    切成 N 段各发一次请求，读到的就是窗口里最新 N×100 条。**标准一点没动**：
    窗口是同一个，筛的还是同一套规则，只是把窗口读完而不是只读顶端。

    `slices <= 1` 时原样返回一段，与不切片逐位相同——所以这是个安全的默认值。

    ## 边界

    子窗口在切点处共享端点（`[t0,t1] [t1,t2] …`），X 的 `start_time`/`end_time`
    都是闭区间，所以落在切点上的那条帖子可能被**两段各取回一次**。这里不去重：
    本函数是纯函数，看不到帖子；调用方按 `platform_post_id` 去重（那里本来
    就要去重，因为落库用的是 `ON CONFLICT DO NOTHING`）。
    """
    if slices <= 1:
        return [(window_start, window_end)]
    step = (window_end - window_start) / slices
    edges = [window_start + step * index for index in range(slices)] + [window_end]
    return [(edges[index], edges[index + 1]) for index in range(slices)]


__all__ = [
    "CandidateSample",
    "SampleDecision",
    "collect_slices",
    "collection_query",
    "collection_window",
    "engagement_score",
    "evaluate_sample",
    "filter_candidates",
    "is_link_only",
]
