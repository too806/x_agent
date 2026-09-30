"""Style Benchmark 的**固定离线语料**。

## 它为什么必须是合成的

工作单第四十节禁止复制高粉账号的原文，第三十二节禁止生成"像某某"的人设。
一份 benchmark 只要装进真实帖子，这两条就同时被违反——而 benchmark 是最容易
被后来的人当作"参考语料"复制走的东西。

所以这里的每一条都是为测试**写**的：句式贴近中文科技内容，但不对应任何
真实账号、真实事件或真实数字。这一点写进 `CORPUS_NOTE`，并由
`check_corpus_is_synthetic` 确认没有外链、没有 @handle。

## 它为什么必须是固定的

整份 benchmark 的价值在于**可复现**：同一份语料必须永远算出同一个 Profile、
同一个 Recipe。一旦语料随挂钟或网络变化，"这次和上次不一样"就永远有两种解释
（代码变了 / 数据变了），benchmark 也就失去了诊断能力。

## 语料与"被拒样本"分开

`SAMPLES` 是**已经进入语料**的样本（它们决定 Profile）。筛选规则的测试需要
一批**被拒**的候选（纯链接、转发、太短、广告），那些放在 `REJECTED` 里，
用 `CandidateSample` 表达。混在一起会让"Profile 基于几条样本"变成一个
没法回答的问题。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.style.corpus import CandidateSample

CORPUS_NOTE = (
    "合成语料：为离线基准编写，不对应任何真实账号、事件或数字。"
    "禁止把它当作可引用的真实语料。"
)


@dataclass(frozen=True)
class Sample:
    """一条已进入语料的样本。

    `structure_type` / `hook_type` 是**已标注**的语义标签——在真实链路里由
    `app/style/annotate.py` 的 LLM 标注产出，这里直接写死，因为 benchmark
    要测的是**统计与推导**是否确定，不是标注质量。
    """

    post_id: str
    content: str
    structure_type: str
    hook_type: str


#: 八条样本，覆盖分类法里的主要结构、hook 与表层开头形态。
#: 覆盖度是刻意的：`structure_distribution` 只有一个类别时，"取最常见的那个"
#: 这条逻辑无论对错都会通过。
SAMPLES: tuple[Sample, ...] = (
    Sample(
        post_id="s1",
        content=(
            "先把判断放在前面：这一轮真正被改变的不是模型能力，是推理成本的量级。"
            "多数团队还在按参数量选型，而选型标准已经变成了每百万 token 的调度开销。"
            "所以别急着换模型，先看你的调度层。"
        ),
        structure_type="opinion_first",
        hook_type="statement",
    ),
    Sample(
        post_id="s2",
        content=(
            "一组观测数字：单次推理的排队时间从 800ms 降到 120ms，"
            "而模型本身的耗时只变了 15%。\n\n"
            "剩下的差距全部来自批处理与调度。这说明瓶颈早就换了位置，"
            "只是监控面板还盯着旧的那个。"
        ),
        structure_type="data_reading",
        hook_type="statistic",
    ),
    Sample(
        post_id="s3",
        content=(
            "很多人说小模型是降级方案。恰恰相反：在长上下文场景里，"
            "小模型配上好的检索比大模型硬塞更稳。前者的错误率低一半，"
            "而后者只是看起来更聪明。"
        ),
        structure_type="counterintuitive",
        hook_type="contrast",
    ),
    Sample(
        post_id="s4",
        content=(
            "如果你要把现有服务接进一个推理网关，按这个顺序做：\n\n"
            "1. 先把超时和重试对齐，否则压测数据没有意义；\n"
            "2. 再把批处理窗口调小，观察尾延迟；\n"
            "3. 最后才动缓存。\n\n"
            "顺序反了，你会花两周去优化一个不是瓶颈的地方。"
        ),
        structure_type="how_to",
        hook_type="list",
    ),
    Sample(
        post_id="s5",
        content=(
            "为什么大家都在谈推理成本，却很少有人谈它的分母？"
            "因为分母是调用量，而调用量取决于产品形态。一个把模型塞进"
            "每一步交互的产品，成本天然比只在关键节点调用的产品高一个数量级。"
        ),
        structure_type="question_answer",
        hook_type="question",
    ),
    Sample(
        post_id="s6",
        content=(
            "「上下文窗口越大越好」这个说法只对了一半。窗口变大之后，"
            "检索质量的下限反而成了决定因素——塞进去的噪声越多，"
            "模型越容易在无关信息上建立关联。"
        ),
        structure_type="opinion_first",
        hook_type="quote",
    ),
    Sample(
        post_id="s7",
        content=(
            "同一条时间线上，三种不同的说法在同时传播。有意思的不是谁对，"
            "而是这三种说法各自服务于什么产品叙事。这一点值得单独记下来。"
        ),
        structure_type="observation",
        hook_type="story",
    ),
    Sample(
        post_id="s8",
        content=(
            "2026 年这一轮的变化不在模型侧。上一轮拼的是参数，这一轮拼的是"
            "单位算力能换来多少有效输出。换句话说，工程能力第一次比模型能力更值钱。"
        ),
        structure_type="contrast",
        hook_type="statistic",
    ),
)


#: 应当被筛选规则拒掉的候选，每条对应一个具体的拒绝理由。
#: 它们是 benchmark 的**反例集**：一个只会说"通过"的筛选器在这里会露馅。
REJECTED: tuple[tuple[CandidateSample, str], ...] = (
    (
        CandidateSample(
            platform_post_id="r-link-only",
            author_username="synthetic",
            content="这个值得看 https://example.invalid/abc",
            likes=500,
            reposts=100,
        ),
        "link_only",
    ),
    (
        CandidateSample(
            platform_post_id="r-repost",
            author_username="synthetic",
            content=SAMPLES[0].content,
            likes=500,
            reposts=100,
            is_repost=True,
        ),
        "repost",
    ),
    (
        CandidateSample(
            platform_post_id="r-too-short",
            author_username="synthetic",
            content="挺好。",
            likes=500,
            reposts=100,
        ),
        "too_short",
    ),
    (
        CandidateSample(
            platform_post_id="r-promo",
            author_username="synthetic",
            content=SAMPLES[1].content + "关注并转发送好礼",
            likes=500,
            reposts=100,
        ),
        "promo",
    ),
    (
        CandidateSample(
            platform_post_id="r-low-likes",
            author_username="synthetic",
            content=SAMPLES[3].content,
            likes=0,
            reposts=0,
        ),
        "low_likes",
    ),
    (
        CandidateSample(
            platform_post_id="r-empty",
            author_username="synthetic",
            content="   ",
            likes=500,
            reposts=100,
        ),
        "empty_content",
    ),
)


__all__ = ["CORPUS_NOTE", "REJECTED", "SAMPLES", "Sample"]
