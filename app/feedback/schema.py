"""反馈层的结构化契约（Phase 5B.4）。

四个对象，**存放位置各不相同**，刻意如此——把"放在哪里"想清楚，
比多写几个字段重要：

| 对象 | 存放位置 | 为什么 |
| --- | --- | --- |
| `SnapshotObservation` | 不落库、不进 checkpoint | 它就是 `post_metrics_snapshot` 的一行，读出来即用 |
| `FeedbackSignal` | 嵌在报告里，不单独存 | 由观察纯函数推出，重算逐位相同（同 `AiStyleSignals`） |
| `FeedbackReport` | 读时现算（CLI / Admin API） | 一次读的产物，不是资产 |
| `FeedbackContext` | **进 checkpoint，不落库** | 它是一次运行"看到了什么反馈"的运行期决策，同 `StyleRecipe` |

`FeedbackContext` 与 `StyleRecipe` 的存放位置一致不是巧合：两者都是
"这次运行拿到的输入摘要"，而不是外部世界的资产。

## 全部字段可空，且没有一处默认 0

除计数本身外，每个可能算不出来的字段都是 `| None`。这不是懒，
是工作单第十节的硬要求：`0` 会被读成"测出来是零"，而我们要表达的是
"没算出来"。仓库里同一条纪律见 `app/style/schemas.py::StyleFit`。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class FeedbackSignalKind(str, Enum):
    """信号种类——**封闭枚举**，不允许自由文本。

    v1 只描述三类互动各自的分布，外加两个"什么也没法说"的诚实出口。
    这里**没有** `QUALITY` / `ENGAGEMENT_SCORE` / `RECOMMENDATION` 这类成员，
    而且不该有：一个综合分或一条"下次该怎么写"的建议，正是工作单
    第十二、十七、二十八节禁止的东西。
    """

    #: 点赞的分布（注意力）。
    ATTENTION = "attention"
    #: 回复 + 引用的分布（对话）。
    CONVERSATION = "conversation"
    #: 转发的分布（扩散）。
    AMPLIFICATION = "amplification"
    #: 三类互动占互动总量的比例分布。
    SHARE = "share"
    #: 样本量低于比较下限：只说 n，不产出任何比较。
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    #: 有观察缺少某个计数：如实记下"读不到"，不按 0 计入。
    MISSING_METRIC = "missing_metric"


class SnapshotObservation(BaseModel):
    """一条快照行的只读视图：**我们自己某条帖子的某一次观察**。

    `observed_at`（我们的抓取时刻）与 `published_at`（来源给的发布时刻）
    是两个不同的时间语义，工作单第九节要求它们始终分开。这里只保留前者
    与派生的 `hours_since_publish`；后者在 `published_post` 上，
    读取时不重复搬进来，免得出现两份可能不一致的副本。
    """

    model_config = ConfigDict(extra="forbid")

    #: `post_metrics_snapshot.id`。信号的可追溯性就是绑在它上面的（工作单第二十五节）。
    snapshot_id: str
    x_post_id: str
    published_post_id: str
    observed_at: datetime | None = None
    #: 观察时刻 − 发布时刻（小时）。由抓取脚本在写入时算好，不可从本行重算。
    hours_since_publish: float | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    impressions: int | None = None
    bookmarks: int | None = None


class FeedbackSignal(BaseModel):
    """一条可解释的反馈信号。**它只描述观察到的分布。**

    ## 每个字段都在防一种具体的误读

    - `statement`：人读的那句话。由代码用固定模板生成，**不含因果词**——
      生成后会过 `assert_no_causal_claim`，命中即编程错误（工作单第三、十九、三十一节）。
    - `evidence`：这条信号依据的快照 id，**必须非空且都真实存在**。
      没有依据就没有信号（工作单第二十五节）。
    - `sample_count`：这条信号依据了多少条观察。任何时候都带着 n，
      因为一个没有 n 的结论无法被判断为"样本太小"。
    - `comparison_group`：与谁比。**v1 恒为 `None`**——我们还没有自己的
      基线，所以只描述分布，不做任何组间比较。
    - `confidence`：**v1 恒为 `None`**。没有经过校准的概率可以给出来，
      编一个数字就是工作单第十节禁止的捏造。
    """

    model_config = ConfigDict(extra="forbid")

    kind: FeedbackSignalKind
    #: 观察性陈述。措辞必须让读者看出"这是观察，不是因果结论"。
    statement: str
    #: 涉及的度量名（`likes` / `conversation` / …）；不适用时为 `None`。
    metric: str | None = None
    #: 该度量的代表值（多数时候是中位数）。未知为 `None`，**不是 0**。
    value: float | None = None
    #: 与谁比较。v1 恒 `None`（见类文档）。
    comparison_group: str | None = None
    sample_count: int = 0
    #: 经过校准的置信度。v1 恒 `None`（见类文档）。
    confidence: float | None = None
    #: 依据的快照 id，非空（工作单第二十五节：没有依据就没有信号）。
    evidence: tuple[str, ...] = Field(default_factory=tuple)
    #: 所属时间档（`0-1h` …）。只有当信号确实按档聚合时才有值。
    window: str | None = None


class FeedbackReport(BaseModel):
    """一次读的完整产物：观察 + 信号 + 这次**没**算什么。

    `limitations` 是必填的、被显式填满的字段，不是可选的备注。工作单第三十五节
    要求"数据不足就不许编"，那么"哪里不足"就必须是报告的一部分，
    而不是留给读者去猜。
    """

    model_config = ConfigDict(extra="forbid")

    sample_count: int = 0
    observations: tuple[SnapshotObservation, ...] = Field(default_factory=tuple)
    signals: tuple[FeedbackSignal, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)


class FeedbackContext(BaseModel):
    """进 checkpoint 的 Writer 上下文：**我们过去发出去的东西后来怎么样了**。

    ## 它是观察，不是指令（工作单第四十二节）

    这个对象里**没有**任何一条过去帖子的正文，也没有"这种写法拿到了多少赞"
    这类可以被当成目标的说法。它只有：样本量、时间档、几类互动的分布、
    以及这份摘要的局限。Writer 拿到的是"我看过结果"，不是"照着这个写"。

    ## 为什么只留 `source_snapshot_ids` 而不是全部观察

    观察可以有很多条，checkpoint 是要被反复读写的东西。信号已经带着自己的
    证据 id，整批 id 再单独留一份，足以让 Run Detail 事后回到原始观察，
    而不必把整张快照表塞进 state。
    """

    model_config = ConfigDict(extra="forbid")

    sample_count: int = 0
    post_count: int = 0
    snapshot_count: int = 0
    latest_observed_at: datetime | None = None
    signals: tuple[FeedbackSignal, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)
    source_snapshot_ids: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """这次运行**没有**任何反馈可用。

        `format_feedback_context` 据此决定要不要印"还没有观察数据"那句话。
        它同时也是"No observations yet"这条中性文本的唯一判据——
        不让调用方各自去判断 `sample_count == 0`（那样两个地方会漂移）。
        """
        return self.snapshot_count == 0 and not self.signals


class PostPerformanceProfile(BaseModel):
    """一条已发布帖子的归因视图（工作单第十三、十四节）。

    它把**三类来源**并排放在一起，且刻意不把它们合成一个分数：

    - 结果：`observations`（我们自己帖子的观察序列）；
    - 交付：`content_length`（当时实际发出去多长）；
    - 决策：`selected_topic` / `topic_trend_score` / `summary of selected_angle` /
      `style_recipe_summary` / `critic_score`——这些是**当时记下来的**运行期决策，
      从 checkpoint 读回，不重算、不改写。

    每一个 checkpoint 读不到的字段都是 `None`，并在 `limitations` 里说明。
    **绝不用一个默认值顶替**：把"checkpoint 被回收了"写成"当时的 trend 分是 0"，
    会让一份残缺的归因看起来像一份完整的。
    """

    model_config = ConfigDict(extra="forbid")

    published_post_id: str
    x_post_id: str | None = None
    published_at: datetime | None = None
    content_length: int | None = None
    run_id: str | None = None
    topic: str | None = None
    selected_topic: str | None = None
    topic_trend_score: float | None = None
    #: 当时选中的观点的一句话摘要（不是完整对象）。
    selected_angle_claim: str | None = None
    selected_angle_confidence: float | None = None
    #: 当时的判断强度（`ViewpointStance` 的枚举值）。同样是**当时记下来的**，
    #: 不重算——观点评估需要证据与当下语境，重跑会得到另一个答案。
    viewpoint_stance: str | None = None
    #: 当时这条观点有没有反方视角（`bool(angle.counterpoints)`）。
    #: `False` 与 `None` 含义相反：前者是"当时确实没有反方视角"（一条真实的观察），
    #: 后者是"读不到当时的观点"（未知）。所以这里**没有** `bool(...)` 兜底。
    counterpoint_awareness: bool | None = None
    #: 当时的结论是否被判为超出证据（`Angle.scope_exceeds_evidence`）。
    scope_exceeds_evidence: bool | None = None
    #: 当时采用的写作结构摘要。
    style_structure: str | None = None
    style_hook: str | None = None
    style_target_length: int | None = None
    #: 当时的 Critic 分数。**它是当时的评审意见，不是结果的质量**。
    critic_score: float | None = None
    observations: tuple[SnapshotObservation, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)


__all__ = [
    "FeedbackContext",
    "FeedbackReport",
    "FeedbackSignal",
    "FeedbackSignalKind",
    "PostPerformanceProfile",
    "SnapshotObservation",
]
