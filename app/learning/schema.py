"""学习准备度层的结构化契约（Phase 5C）。

这一层回答的问题只有一句：

    我过去的反馈，**够不够可靠**，可以拿去做未来的学习材料？

它**不回答**"以后该怎么写"。所以这里没有一条指令、没有一个"最佳实践"、
没有 `apply_learning`——只有观察、比较、以及一个确定性的准入开关。

## 五个对象，存放位置各不相同（沿用 5B.4 的纪律）

| 对象 | 存放位置 | 为什么 |
| --- | --- | --- |
| `PostPerformanceRecord` | 不落库 | 它是 `published_post` + 快照 + checkpoint 的**派生视图**，实时组合 |
| `CohortDefinition` | 不落库 | 一次分组的产物，重算逐位相同 |
| `ObservedComparison` | 不落库 | 分组的纯函数 |
| `LearningEvidence` | 不落库、可进 checkpoint（**本阶段不进**） | 它仍是观察的纯函数 |
| `LearningReadiness` | 不落库 | 由上面几个派生，确定性 |

**本阶段不新增任何数据库列或表**（工作单第四、五节）。`structure_type` / `hook_type` /
`stance` 这些已经能从 checkpoint 读回，就不建 `*_history`；一旦复制成列，就会出现
第二个真相来源，然后某天悄悄漂掉——正是前面三个阶段各自明确拒绝过的东西。

## 分析单位是**帖子**，不是快照（本阶段最容易写错的一条）

同一条帖子的 1h / 6h / 24h 三条快照是**同一条帖子的重复观察**，不是三个样本。
把三条当成三份证据，等于把同一句话数了三遍，是小样本上最容易造出"显著差异"的方式。
所以：

- 每个帖子在一个观察窗口里只贡献**一个**值（该窗口内最新那条观察）；
- `sample_count` 一律指**帖子数**，不是快照数；
- `1 post / 2 snapshots` 因此是 n=1，正确地够不上比较下限。

## 未知一律是 `None`，永远不是 0

同 `app/feedback/schema.py`：`None` 表示"没读到"，`0` 表示"读到的是零"。两者在本层
含义相反——一个缺失的分量如果被补成 0，会把它拉低到分布的下沿，然后变成一条
"这一类表现更差"的假结论。本模块**没有一处默认值写成 0**。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.feedback.schema import SnapshotObservation


class PerformanceMetric(str, Enum):
    """performance vector 的三个分量——**封闭枚举**。

    与 `app/feedback/schema.py::FeedbackSignalKind` 的三个互动种类**同名同义**，
    刻意不复用那个枚举：那是"一条信号是什么"的枚举，还带着 `SHARE` /
    `INSUFFICIENT_SAMPLE` 这些**报告层**的成员；本枚举只用于比较，多一个成员
    都会让"按指标比较"这件事出现无效分支。

    这里**没有** `score` / `engagement` / `quality` 这类成员，而且不该有：
    一个综合分正是工作单第九节禁止的"用一个数代表好坏"。
    """

    ATTENTION = "attention"
    CONVERSATION = "conversation"
    AMPLIFICATION = "amplification"


class CohortDimension(str, Enum):
    """可以据以分组的维度——**封闭枚举**。

    每一项都必须有**确定性的取值来源**（来自 StyleRecipe / Viewpoint / 正文长度），
    不能靠语言模型现分。`topic class` 刻意不在表内：本仓库没有确定性的选题分类
    （`app/agent/trends.py` 只给分数，不给类别），让 LLM 去分就是往一条"看起来
    是数据"的结论里掺入不可复现的判断。缺这一项会写进 limitations，不假装没有。
    """

    STRUCTURE_TYPE = "structure_type"
    HOOK_TYPE = "hook_type"
    STANCE = "stance"
    LENGTH_BUCKET = "length_bucket"
    COUNTERPOINT_AWARENESS = "counterpoint_awareness"
    SCOPE_EXCEEDS_EVIDENCE = "scope_exceeds_evidence"


class LearningEvidenceKind(str, Enum):
    """一条学习证据的种类——**封闭枚举**，不允许自由文本。

    正向的只有 `COHORT_DIFFERENCE`；其余四项都是"什么也没法说"的诚实出口。
    这里**没有** `RECOMMENDATION` / `RULE` / `ACTION`，而且不该有：
    一条"下次该怎么写"的建议正是工作单第二十节禁止的东西。
    """

    #: 两个 cohort 在同一个窗口、同一指标上的分布差异。
    COHORT_DIFFERENCE = "cohort_difference"
    #: 帖子数低于比较下限：只说 n，不产出任何比较。
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    #: 某个指标在某个 cohort 里读数不足：如实记下，不补 0。
    MISSING_METRIC = "missing_metric"
    #: 观察跨越了不同窗口：拒绝直接比较。
    WINDOW_MISMATCH = "window_mismatch"
    #: 比较不成立（两组重叠 / 取值相同 / 依据不足）。
    INVALID_COMPARISON = "invalid_comparison"


class PerformanceVector(BaseModel):
    """一条帖子在**某一个观察窗口**下的三类互动计数。

    三个分量**独立、不合成**（工作单第八、九节）。理由：一条帖子可能点赞高、
    回复低，另一条点赞一般、回复高，这两种结果对内容价值的含义不同；
    先合成一个"热度"再讨论，等于把这个区别提前抹掉。

    映射沿用第四层 `app/feedback/metrics.py` 的定义，**不另立一套**：

        attention     = likes
        conversation  = replies + quotes
        amplification = reposts

    每个分量都可空：`None` = 该窗口内没有一条读数，**不是 0**。
    """

    model_config = ConfigDict(extra="forbid")

    attention: int | None = None
    conversation: int | None = None
    amplification: int | None = None

    @property
    def is_empty(self) -> bool:
        """三个分量全都没读到。"""
        return self.attention is None and self.conversation is None and self.amplification is None

    def value_of(self, metric: PerformanceMetric) -> int | None:
        """按指标取分量。**未知返回 `None`，不返回 0**。"""
        return getattr(self, metric.value)


class PostPerformanceRecord(BaseModel):
    """一条已发布帖子的**只读派生视图**：结果 ‖ 交付 ‖ 决策（工作单第四节）。

    它是 `app/feedback/profile.py::build_post_performance_profile` 的输出再加两样东西：

    - `performance`：在**指定观察窗口**下取到的 `PerformanceVector`；
    - `length_bucket`：由实际发布长度推出的确定性档位。

    三块并排放着，**不合并、不加权、不排序**。`critic_score` 是**当时的评审意见**，
    不是结果的质量；`trend_score` 是**当时选它时的分数**，不是现在的趋势。
    读回的都是"当时记下来是多少"，一个字都不重算。

    每一个 checkpoint 读不到的字段都是 `None`，并在 `limitations` 里说明。
    **绝不用默认值顶替**："checkpoint 被回收了"与"当时 trend 分是 0"是两件事。
    """

    model_config = ConfigDict(extra="forbid")

    published_post_id: str
    x_post_id: str | None = None
    published_at: datetime | None = None
    run_id: str | None = None

    # --- 决策（来自 checkpoint 的当时值） ---
    topic: str | None = None
    trend_score: float | None = None
    structure_type: str | None = None
    hook_type: str | None = None
    effective_target_length: int | None = None
    stance: str | None = None
    counterpoint_awareness: bool | None = None
    scope_exceeds_evidence: bool | None = None
    critic_score: float | None = None

    # --- 交付（来自 published_post.body） ---
    content_length: int | None = None
    length_bucket: str | None = None

    # --- 结果（来自快照表，按窗口取值） ---
    observation_window: str
    performance: PerformanceVector = Field(default_factory=PerformanceVector)
    #: 该帖子的完整观察序列，供追溯（evidence → snapshot → post → x_post）。
    observations: tuple[SnapshotObservation, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)


class CohortDefinition(BaseModel):
    """一个比较组：**同一维度、同一取值、同一观察窗口**的一批帖子（工作单第十、十一节）。

    `sample_count` / `time_window` / `selection_rule` 是必填的，不是可选的备注。
    没有这三样，"n=7 的 question hook 比 n=14 的 statement hook 好"这句话
    就无法被追问：样本是多少、看的是发布后多久、怎么选的组。工作单第十一节
    要求的正是"能追问"。

    两个 cohort 的 topic 组成不同时，差异可能来自选题而非该维度——这**不会被
    静默忽略**，而是写进 `limitations`（本仓库没有确定性的选题分类，无法控制它）。
    """

    model_config = ConfigDict(extra="forbid")

    dimension: CohortDimension
    value: str
    observation_window: str
    #: 人读的选取规则：说明按什么分组、窗口固定在哪、分析单位是什么。
    selection_rule: str
    sample_count: int = 0
    post_ids: tuple[str, ...] = Field(default_factory=tuple)
    snapshot_ids: tuple[str, ...] = Field(default_factory=tuple)
    #: 本组涉及的不同 topic 文本（有值才记）。
    topics: tuple[str, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)


class CohortStats(BaseModel):
    """一个 cohort 在某指标上的分布。

    `median` / `p25` / `p75` 是主口径；`mean` 只是**辅助**（工作单第十四节）。
    X 内容表现极易出现"一个异常高值 + 一堆普通值"，均值会被那一个值带走，
    而中位数不会。`sample_count` 是**有该指标读数的帖子数**，可能小于
    cohort 的帖子数——差的那些就是读数不足，**不补 0**。
    """

    model_config = ConfigDict(extra="forbid")

    value: str
    sample_count: int = 0
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    #: 辅助口径，仅作参考。
    mean: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    #: 被 IQR 栅栏标出的离群点**数量**。离群点不删除，只标记（工作单第十五节）。
    outlier_count: int = 0
    post_ids: tuple[str, ...] = Field(default_factory=tuple)
    snapshot_ids: tuple[str, ...] = Field(default_factory=tuple)


class ObservedComparison(BaseModel):
    """两个 cohort 在某指标上的**观察性**比较（工作单第十三节）。

    它**永远**只是观察：

    - `valid=False` 时没有任何结论，只有为什么不比（两组重叠 / 读数不足 / 取值相同）；
    - `statement` 由固定模板生成，措辞是"在当前样本中观察到……更高/更低"，
      经 `assert_no_causal_claim` 检查，命中即编程错误；
    - 这里没有 `winner` / `better` / `recommendation` 字段，而且不该有。

    `observed_difference` 是 `b.median - a.median`（可为负、可为 `None`）。
    它是给人看的方向提示，不是"效果大小"——两组样本量可能不同，窗口内的
    观测也可能受内容之外的变量影响。
    """

    model_config = ConfigDict(extra="forbid")

    dimension: CohortDimension
    observation_window: str
    metric: PerformanceMetric
    group_a: CohortStats
    group_b: CohortStats
    observed_difference: float | None = None
    #: 比较是否成立（两组各 n>=下限、同窗口、互不重叠）。
    valid: bool = False
    #: 不成立的原因；成立时为 `None`。
    invalid_reason: str | None = None
    #: 观察性陈述。`valid=False` 时说明为什么不比，不含任何结论。
    statement: str = ""
    source_snapshot_ids: tuple[str, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def comparison_group(self) -> str:
        """比较组的人读标识：`维度: A vs B @ 窗口 · 指标`。"""
        return (
            f"{self.dimension.value}: {self.group_a.value} vs {self.group_b.value}"
            f" @ {self.observation_window} · {self.metric.value}"
        )


class LearningEvidence(BaseModel):
    """一条学习证据（工作单第十九节）。**它依旧是观察，不是指令。**

    可追溯性是硬要求（工作单第十八节）：任何一条证据都必须能沿
    `comparison → post_ids → snapshot_ids → x_post_id` 一路回到原始观察。
    所以 `source_snapshot_ids` 之外还给出 `source_post_ids` 与 `source_x_post_ids`。

    `confidence` **恒为 `None`**：本仓库没有经过校准的概率模型，编一个数字
    就是捏造（同 `app/feedback/schema.py::FeedbackSignal` 的理由）。
    "n=8 比 n=11 的中位数高"不构成一个可以量化信心的估计。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    type: LearningEvidenceKind
    #: 人读的观察性陈述。由代码用固定模板生成，不含因果词、不含指令词。
    statement: str
    #: 这条证据依据了多少条帖子（**不是快照数**）。
    sample_count: int = 0
    #: 与谁比。不适用时为 `None`。
    comparison_group: str | None = None
    #: 所属观察窗口。
    observation_window: str | None = None
    metric: str | None = None
    dimension: str | None = None
    source_snapshot_ids: tuple[str, ...] = Field(default_factory=tuple)
    source_post_ids: tuple[str, ...] = Field(default_factory=tuple)
    source_x_post_ids: tuple[str, ...] = Field(default_factory=tuple)
    #: 经过校准的置信度。**恒为 `None`**（见类文档）。
    confidence: float | None = None
    limitations: tuple[str, ...] = Field(default_factory=tuple)


class ReadinessCheck(BaseModel):
    """准入开关的一条判据。**由代码判定，绝不交给 LLM**（工作单第二十四节）。"""

    model_config = ConfigDict(extra="forbid")

    #: 稳定的机器名：`sample_count` / `window_consistent` / `metrics_present` /
    #: `comparison_valid`。写死字符串是因为测试与前端按它取用。
    name: str
    passed: bool
    detail: str = ""


class LearningReadiness(BaseModel):
    """确定性 learning gate 的结果（工作单第二十四、三十七节）。

    `learning_ready = True` **只**表示"可以作为未来学习材料"，**不**表示
    "立即改变 Agent"。所以：

    - `automatic_modification` **恒为 `False`**，而且是这里的字段而不是调用方的
      约定——一个恒假的字段会被测试钉住，而一句约定不会；
    - 没有 `apply_learning()` 这样的方法存在（`evals/learning/selftest.py` 钉死）。

    四个条件必须**同时**成立：样本量、窗口一致、指标齐全、比较成立。
    """

    model_config = ConfigDict(extra="forbid")

    learning_ready: bool = False
    #: `insufficient_sample` / `inconsistent_window` / `missing_metrics` /
    #: `invalid_comparison` / `ready`。
    reason: str = "insufficient_sample"
    observation_window: str
    #: 在窗口内有读数的**帖子数**。
    sample_count: int = 0
    #: 窗口内有读数的帖子总数（与 sample_count 同源，留作对照）。
    post_count: int = 0
    checks: tuple[ReadinessCheck, ...] = Field(default_factory=tuple)
    #: **恒为 `False`**。见类文档。
    automatic_modification: bool = False
    limitations: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def usable_for_future_learning(self) -> bool:
        """工作单第二十四节的同名开关，就是 `learning_ready` 本身。

        做成 property 而不是第二个字段：两个字段会有两个真相来源，
        而它们必须永远相等。
        """
        return self.learning_ready


class LearningReport(BaseModel):
    """一次学习的完整产物：记录 + 分组 + 比较 + 证据 + 准入开关（工作单第三十九节）。

    `limitations` 是必填的、被显式填满的字段，不是可选备注。工作单第三十一节
    要求"数据不足就不许编"，那么"哪里不足"就必须是报告的一部分。
    """

    model_config = ConfigDict(extra="forbid")

    observation_window: str
    records: tuple[PostPerformanceRecord, ...] = Field(default_factory=tuple)
    cohorts: tuple[CohortDefinition, ...] = Field(default_factory=tuple)
    comparisons: tuple[ObservedComparison, ...] = Field(default_factory=tuple)
    evidence: tuple[LearningEvidence, ...] = Field(default_factory=tuple)
    readiness: LearningReadiness
    limitations: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def post_count(self) -> int:
        return len(self.records)

    @property
    def valid_comparisons(self) -> tuple[ObservedComparison, ...]:
        return tuple(c for c in self.comparisons if c.valid)


__all__ = [
    "CohortDefinition",
    "CohortDimension",
    "CohortStats",
    "LearningEvidence",
    "LearningEvidenceKind",
    "LearningReadiness",
    "LearningReport",
    "ObservedComparison",
    "PerformanceMetric",
    "PerformanceVector",
    "PostPerformanceRecord",
    "ReadinessCheck",
]
