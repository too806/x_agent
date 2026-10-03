"""风格层的结构化契约：Profile 的计算结果、StyleRecipe、StyleFit。

这三个东西的**存放位置**不同，刻意如此：

- `StyleProfileFeatures`：offline 计算的中间结果，写进 `style_profile` 表，
  不进 checkpoint（它是语料资产，不是运行期状态）。
- `StyleRecipe`：一次 Run 的**运行期决策**，进 checkpoint，**不落库**
  （见 alembic c3d8a45e7f21 的说明）。
- `StyleFit`：Critic 的一次判断，嵌在 `CriticResult` 里，随 checkpoint 走。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.style.taxonomy import HookType, StructureType

#: Profile 的算法版本。改了特征定义就要进位——否则新旧 Profile
#: 会被当成同口径直接比较。
#:
#: 1.1：词表新增 `CLICKBAIT_PHRASES`，并加入内部编号泄漏的检测原语
#: （`find_internal_refs`）。原语本身不进 `StyleProfile`，但它与套话词表
#: 同属一份经验口径，跟着一起进位才不会让两份报告被误读成"稿子变了"。
FEATURE_VERSION = "1.1"


class DensityLevel(str, Enum):
    """信息密度档位。**档位**而不是"每百字几个实体"：recipe 是给 Writer 的
    约束，Writer 需要的是"写密一点"这个可执行的程度，不是一个要它自己
    换算的连续值。连续值留在 Profile 里，供人核对。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StanceStrength(str, Enum):
    SOFT = "soft"
    MODERATE = "moderate"
    STRONG = "strong"


class EvidenceRequirement(str, Enum):
    """对证据的**最低**要求。Critic 用它判 `evidence_binding` 是否达标。"""

    OPTIONAL = "optional"
    REQUIRED = "required"
    STRICT = "strict"


class Tone(str, Enum):
    NEUTRAL = "neutral"
    DIRECT = "direct"
    ANALYTICAL = "analytical"
    CONVERSATIONAL = "conversational"


class EndingType(str, Enum):
    CONCLUSION = "conclusion"
    QUESTION = "question"
    CALL = "call"
    OBSERVATION = "observation"


class StyleProfileFeatures(BaseModel):
    """从一批样本算出的统计特征（`app/style/profile.py` 的产物）。

    字段名与 `app/models/style_profile.py::StyleProfile` 的列**逐一对齐**：
    一个是计算结果的形状，一个是它的持久化。对齐之后，"存进去的和算出来的
    不一致"这类错误在类型层面就不成立。

    每个可能算不出来的数都是 `float | None`：corpus 为空、分母为 0 时是
    `None`，不是 `0.0`。0 会被读成"测出来是零"，而这两件事在聚合时差别是全部。
    """

    model_config = ConfigDict(extra="forbid")

    sample_count: int = 0

    #: 长度分位（字符数）。
    median_length: float | None = None
    p25_length: float | None = None
    p75_length: float | None = None
    median_paragraphs: float | None = None
    mean_sentence_count: float | None = None

    #: 每 100 字。
    emoji_density: float | None = None
    question_density: float | None = None
    number_density: float | None = None
    #: 拉丁字母占非空白字符的比例（0-1）。
    english_ratio: float | None = None

    #: 占比分布，各自和为 1（样本为空时为空 dict）。
    structure_distribution: dict[str, float] = Field(default_factory=dict)
    hook_distribution: dict[str, float] = Field(default_factory=dict)
    opening_distribution: dict[str, float] = Field(default_factory=dict)
    closing_distribution: dict[str, float] = Field(default_factory=dict)
    #: {"p25": .., "p50": .., "p75": ..}
    sentence_length_distribution: dict[str, float] = Field(default_factory=dict)


class StyleRecipe(BaseModel):
    """**这次运行采用什么写作结构**——一组生成约束（工作单第十九节）。

    ## 它是约束，不是正文

    Writer 收到这个对象，**永远收不到样本原文**。这是工作单第二十二节的
    硬要求：检索式模仿会让 Agent 抄的是**具体某个人**的句子，而不是从多个
    样本里提炼出的、可解释、可复用、可调整的结构。

    ## 每个参数都要有依据

    `reasons` 里的每一条都要能回到一个具体的数（Profile 里的某个统计量，
    或 EvidencePack 里的某个可见事实）。判断"为什么选对比结构"的答案
    不能是"模型觉得合适"——那既不可复核，也不可调。

    ## 为什么不做成一大串连续参数

    recipe 是给 Writer 的**可执行约束**。连续值（"目标长度 187.3 字"）既
    不可执行，又暗示了一种并不存在的精度。所以长度给档位化的目标值，
    密度/立场/语气/结尾给有限枚举。连续统计量留在 Profile 里供人核对。
    """

    model_config = ConfigDict(extra="forbid")

    structure_type: StructureType = StructureType.UNCLASSIFIED
    hook_type: HookType = HookType.UNCLASSIFIED
    #: **语料说典型的一条多长**（字符）。来自 Profile 的 median_length，
    #: 收敛到 `config/style.yaml` 的 recipe 区间。
    #:
    #: 它**只反映语料观察**，永远不因为平台的硬上限而被改写——
    #: 一改写，`reasons` 里那句"语料长度中位数是 180 字"就与字段自相矛盾。
    observed_target_length: int | None = None
    #: **这次实际让 Writer 写的长度上界**，即 `persona.max_length`。
    #:
    #: 命名刻意不叫 `max_length`：在 `StyleRecipe` 里单说 `max_length`
    #: 会被读成"这个 recipe 自身文本的长度上限"，而它讲的是**正文**的上限。
    #: 它同时是 guardrail 与发布路径实际执行的那条硬线（`nodes/guardrail.py`、
    #: `services/publish.py`），所以叫它"硬上限"是准确的。
    hard_max_length: int | None = None

    @property
    def effective_target_length(self) -> int | None:
        """**这次实际执行的目标长度** = 语料目标 ∩ 平台硬上限。

        这里必须把两件事分开，因为它们是两种不同的来源：

        - `observed_target_length` 是**语料观察**（"这些人平时写 180 字"）；
        - `hard_max_length` 是**平台约束**（"我们最多发 140 字"）。

        压成同一个数会让一个真实的容量冲突看起来像一条关于语料的结论——
        而语料从没说过"应该写 140 字"。所以三者的关系是显式的：

            observed_target_length = 180   （语料说的）
            hard_max_length        = 140   （平台允许的）
            effective_target_length= 140   （因此实际执行的）

        **不把它存成一个字段**：它完全由另外两个推出来（`min`），
        存下来只会多一个将来会与它们不一致的副本。

        `observed_target_length` 为 `None` 时返回 `None`——**不拿硬上限冒充
        目标长度**。"没有语料依据"与"目标就是上限"是两个不同的陈述，
        后者会凭空给 Writer 一个语料从未支持的长度。
        """
        if self.observed_target_length is None:
            return None
        if self.hard_max_length is None:
            return self.observed_target_length
        return min(self.observed_target_length, self.hard_max_length)

    @property
    def is_length_capped(self) -> bool:
        """语料目标是否**真的**被平台上限压过。

        Run Detail 与 Writer prompt 都据此决定要不要多写一句解释。
        恒显示那句话会让"两者本来就一致"的运行也背上一个不存在的冲突。
        """
        return (
            self.observed_target_length is not None
            and self.hard_max_length is not None
            and self.hard_max_length < self.observed_target_length
        )
    density: DensityLevel = DensityLevel.MEDIUM
    stance_strength: StanceStrength = StanceStrength.MODERATE
    evidence_requirement: EvidenceRequirement = EvidenceRequirement.REQUIRED
    tone: Tone = Tone.NEUTRAL
    ending_type: EndingType = EndingType.CONCLUSION

    #: 每个选择的依据，人能回原始统计核对。
    reasons: list[str] = Field(default_factory=list)
    #: 这一版 recipe 依据的 Profile（形如 `corpus@2026-09-30T11:00:00+00:00`）。
    #: Run Detail 事后能据此回到那一版 Profile——没有它，一个 recipe
    #: 就是一组无法复核的参数。
    profile_ref: str = ""


class StyleFit(BaseModel):
    """Critic 对"稿子是否符合目标 recipe"的分项判断（工作单第二十三节）。

    ## 判的是**符合目标**，不是"像不像某个账号"

    工作单第二十三、三十二节把这条写成硬约束。所以每一项都是
    "与 recipe 的某条约束是否一致"，没有"这个人会不会这么写"这类判断。

    ## 为什么全部是 `float | None` 而不是 `float = 0.0`

    同文件里 `CriticResult` 的旧分项用 `0.0` 默认值。这里刻意不同：
    `0.0` 把"模型没回答"和"模型判为最差"压成同一个值，
    而这两件事在报告里必须能分开——前者是数据缺失，后者是一个判断。
    仓库里同一条纪律见 `evals/values.py` 的 `UNAVAILABLE`。

    全部可空还解决一个更硬的问题：模型返回一个**残缺**的 `style_fit` 时，
    任何必填字段都会让**整份 Critic 响应** validation 失败——
    一个分项缺失不该升级成整轮评审作废。
    """

    model_config = ConfigDict(extra="forbid")

    #: 结构是否符合 recipe.structure_type。
    structure_match: float | None = Field(None, ge=0.0, le=10.0)
    #: 开头是否符合 recipe.hook_type。
    hook_match: float | None = Field(None, ge=0.0, le=10.0)
    #: 信息密度是否符合 recipe.density。
    density_match: float | None = Field(None, ge=0.0, le=10.0)
    #: 长度是否落在 `recipe.effective_target_length` 附近。
    #: **按实际执行的长度判**，不按语料目标：Critic 手上有的是这一轮真正
    #: 被要求写多长，拿一个平台不允许的长度去扣分，等于罚它没做到一件
    #: 它本来就被禁止做的事。
    length_match: float | None = Field(None, ge=0.0, le=10.0)
    #: 语气是否符合 recipe.tone。
    tone_match: float | None = Field(None, ge=0.0, le=10.0)


#: 确定性 AI 味辅助信号的字段名。它们**不是**"AI 检测器"的分数，
#: 只是几个能被复算的计数（工作单第二十五节）。集中列出来，
#: 让"哪些数进了辅助信号"是一个能一眼看完的问题。
AI_STYLE_SIGNAL_NAMES: tuple[str, ...] = (
    "template_opener_count",
    "empty_connective_count",
    "false_authority_count",
    "generic_phrase_count",
    "hedge_count",
    "repeated_opener_count",
)


class AiStyleSignals(BaseModel):
    """一条候选正文的确定性 AI 味辅助信号。

    **不进 state、不落库、不混进 LLM 的分数。** 它是 `content` 的纯函数
    （见 `app/style/features.py`），需要时重算即可，重算结果逐位相同；
    存一份只会多制造一个将来会漂移的来源。

    它的用途只有一个：作为**辅助信号**出现在 Critic 的输入与 Run Detail 里，
    让"这条稿子套话很多"这件事有一个可复算的依据，而不是全靠一次
    不可复现的 LLM 打分。**不做门禁**——把它做成门禁就等于宣称
    这几个词表能判定"这是 AI 写的"，而它不能。
    """

    model_config = ConfigDict(extra="forbid")

    char_count: int = 0
    template_opener_count: int = 0
    empty_connective_count: int = 0
    false_authority_count: int = 0
    generic_phrase_count: int = 0
    hedge_count: int = 0
    repeated_opener_count: int = 0
    #: 命中的套话原文，方便人核对"这个也算套话？"
    generic_phrases: list[str] = Field(default_factory=list)


__all__ = [
    "AI_STYLE_SIGNAL_NAMES",
    "FEATURE_VERSION",
    "AiStyleSignals",
    "DensityLevel",
    "EndingType",
    "EvidenceRequirement",
    "StanceStrength",
    "StyleFit",
    "StyleProfileFeatures",
    "StyleRecipe",
    "Tone",
]
