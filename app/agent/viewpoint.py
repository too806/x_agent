"""确定性的观点评估：一个判断"站不站得住"与"值不值得说"。

工作单第十一、十二节把话说死了：

> 不要让 LLM 单独决定最终 confidence。
> 不要：LLM：这个观点 9.4 分 但无法复算。

所以本模块是一个**纯函数库**（形状同 `app/agent/trends.py`）：没有 IO、没有随机、
没有 `datetime.now()`、不读配置。同一份输入必然得到同一组数，数变了必然能追到
某个输入字段的变化。

LLM 的位置在本模块之外：它负责**产生判断**（claim）、**写出推理**（reasoning）、
**自我反驳**（counterpoints / assumptions / risk）与**申报**（claim_scope /
stance）。它的输出里**没有任何分数字段**（见 `app/agent/schemas.py::AngleDraft`）。
申报是声明不是分数——不能加分，只能触发上限与参考信号。

## 两个分数答两个不同的问题

| 数 | 答的问题 | 什么时候用 |
| --- | --- | --- |
| `confidence` | 这个判断**站不站得住** | 决定 stance 上界；写进 Run Detail 让审核员判断可信度 |
| `viewpoint_score` | 这个判断**值不值得拿出来说** | 排序、选一个 |

两者都**与 `trend_score` 无关**（工作单第十三节）：一个话题很热但观点很弱
（`trend_score` 高、`viewpoint_score` 低）必须被允许，反之亦然。

`novelty` **不在 `confidence` 的权重里**，这是结构性的（工作单第十五节）：
一个"没人想到过"的判断，其证据并不比一个耳熟能详的判断更多。把新颖性混进
置信度，等于允许"大胆但没证据"冒充"有把握"。

## 这个模块不知道的事情（不要假装知道）

- **不是全网语义检索**。工作单第三十一节明确禁止声称"这个观点是全网原创"。
  我们能算的只是"是不是在复述手头的事实/共识/同批另一个观点"。
- **不知道一个判断是不是真的、是不是有洞察力**。那是人审与 Critic 的事。
- **不知道一个反方是真反对还是稻草人**。只能检查它有没有绑到真实事实，
  所以 reason 里写"已绑定到事实"而不是"有效反方"。
- **不知道证据范围是否真的支持一个行业级判断**。scope 阶梯是计数代理，
  不是语义推断——所以 reason 里把用到的数字写出来，让代理是可见的。

判不出来时返回 `None` 并在 reasons 里说明，**不填 0**：把"没算出来"写成 0
会被下游读成"算出来是最差的"，那是两种完全不同的结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from app.agent.schemas import ClaimScope, ViewpointStance
from app.agent.trends import LINK_ONLY_RE, SPREAD_FULL_AUTHORS
from app.style import features
from app.style.spans import SHARED_SPAN, corpus_spans, overlapping_spans

# --- 证据支撑 ---------------------------------------------------------------

#: 几条绑定事实算"支撑充分"。一次 `extract_evidence` 通常产出 3–8 条事实，
#: 4 是"这个判断不止站在一两个观察上"的位置。
#:
#: 刻意用**线性**饱和而不是 trends 里 engagement 那种 log1p：互动量是长尾的
#: （500 与 50000 差两个数量级），事实条数不是（3 与 8 是同一个量级）。
#: 在这么窄的区间上取对数，只会把本来有区分度的差别压平。
EVIDENCE_SUPPORT_SATURATION_FACTS = 4

#: 支撑度里"条数"与"覆盖面"的权重。覆盖面是**少数项**是刻意的：
#: 把证据包里每一条都引上，与"把整个证据包复述了一遍"高度相关，
#: 而那正是 novelty 在盯的反面。让覆盖面占主导会奖励复述。
EVIDENCE_SUPPORT_FACTS_WEIGHT = 0.65

#: 单一来源的支撑度上限。与 `trends.SINGLE_SOURCE_SCORE_CAP` 是同一个意思：
#: 只有一条来源时，量的多少不能替质的不足说话。
#: 取 0.45 而不是 0.40：这里它是五项之一，不是总分本身。
SINGLE_SOURCE_EVIDENCE_CAP = 0.45

# --- 来源多样性 -------------------------------------------------------------

#: 不同来源数的满值。
SOURCE_DIVERSITY_FULL_SOURCES = 3

#: 作者比来源重：同一位作者的三条来源仍然是**一个声音**。
SOURCE_DIVERSITY_AUTHOR_WEIGHT = 0.60

#: 材料完全没有作者信息时的多样性上限。不能让"未知作者"成为通往高分的捷径。
UNKNOWN_AUTHOR_DIVERSITY_CAP = 0.60

# --- 反方 -------------------------------------------------------------------

#: 绑定到事实的反方，一条记满分。
COUNTERPOINT_BOUND_CREDIT = 1.0
#: 只是写了一句、没绑事实的反方。给 0.35 而不是 0：它仍是一次真实的认知动作，
#: 只是不可核验。也不是 0.5——那会让"写两条不绑的"等于"绑一条"。
COUNTERPOINT_DECLARED_CREDIT = 0.35
#: 两条绑定的反方即视为满分。
COUNTERPOINT_FULL_COUNT = 2

# --- 未知压力 ---------------------------------------------------------------

#: 证据包自报 4 处未知即视为压力满。
UNKNOWN_SATURATION = 4
#: 自报项能贡献的最大压力。见 `score_unknown_pressure` 为什么用 max 而不是加权和。
UNKNOWN_COUNT_WEIGHT = 0.5

# --- 置信度 -----------------------------------------------------------------

#: 四项权重，和为 1。工作单第十一节的原话是"这个判断是否有足够证据支持"，
#: 所以 evidence_support 占主导；支撑的**独立性**次之（三位作者的三条事实
#: 与一位作者的三条事实不是一回事）；反方再次（见过对手的判断比没见过更稳）；
#: 未知压力最后——它是这一组里最粗的测量。
#:
#: **novelty 不在里面**，这是工作单第十五节的硬要求，不是疏忽。
CONFIDENCE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("evidence_support", 0.40),
    ("source_diversity", 0.25),
    ("counterpoint_awareness", 0.20),
    ("unknown_certainty", 0.15),
)

#: 单一来源的置信度上限。推导出来的一个有用性质：单一来源的观点
#: **永远到不了 moderate**（见 STANCE 门槛表）。
SINGLE_SOURCE_CONFIDENCE_CAP = 0.40

#: 一条反方都没回应的置信度上限。`app/style/recipe.py::_choose_stance` 已经
#: 写下同一条政策（"不做没有对手的强判断"），这里把它变成可复算的数。
NO_COUNTERPOINT_CONFIDENCE_CAP = 0.65

# --- 观点分 -----------------------------------------------------------------

#: 五项权重，和为 1。这是"值不值得拿出来说"的排序：
#: 站得住是入场券（最大单项），具体与新颖决定它是不是"值得说"而不是"复述"，
#: 多样性与反方是支撑性品质。
VIEWPOINT_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("evidence_support", 0.30),
    ("source_diversity", 0.15),
    ("specificity", 0.20),
    ("novelty", 0.20),
    ("counterpoint_awareness", 0.15),
)

#: novelty 的证据门控满值。novelty 在进入总分前会乘以
#: `min(1, evidence_support / 这个值)`——见 `score_viewpoints`。
NOVELTY_GATE_FULL_EVIDENCE = 0.60

#: 支撑度低于这个值时，观点分被压到 LOW_SUPPORT_VIEWPOINT_CAP 以下。
MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE = 0.35
LOW_SUPPORT_VIEWPOINT_CAP = 50.0

# --- 具体性 -----------------------------------------------------------------

SPECIFICITY_FULL_ENTITIES = 3
SPECIFICITY_FULL_NUMBERS = 2
GENERIC_PHRASE_PENALTY = 0.25
HEDGE_PENALTY = 0.25

# --- 立场上界 ---------------------------------------------------------------

STRONG_MIN_CONFIDENCE = 0.75
MODERATE_MIN_CONFIDENCE = 0.55
CAUTIOUS_MIN_CONFIDENCE = 0.35
#: 与 trends 的合格门槛取同一个数、同一个理由：单一来源/单一作者不是量的问题。
MIN_SOURCES_FOR_STRONG = 2
MIN_AUTHORS_FOR_STRONG = 2
UNKNOWN_PRESSURE_CEILING_FOR_MODERATE = 0.5

STANCE_ORDER: tuple[ViewpointStance, ...] = (
    ViewpointStance.UNCERTAIN,
    ViewpointStance.CAUTIOUS,
    ViewpointStance.MODERATE,
    ViewpointStance.STRONG,
)
_STANCE_INDEX = {stance: index for index, stance in enumerate(STANCE_ORDER)}

# --- 范围 -------------------------------------------------------------------

#: 各个 scope 的宽度。用来比较"申报的范围"与"证据能支撑的范围"。
CLAIM_SCOPE_BREADTH: Mapping[ClaimScope, int] = {
    ClaimScope.FACT: 0,
    ClaimScope.CASE: 1,
    ClaimScope.SEGMENT: 2,
    ClaimScope.INDUSTRY: 3,
    ClaimScope.SOCIETY: 4,
}

#: 证据能支撑到某个 scope 所需的 (来源数, 作者数) 门槛，(scope, 来源, 作者)。
#:
#: `SOCIETY` 的门槛**故意设成一次运行到不了的位置**（一次 X 检索拿不到六个
#: 互相独立的来源）：这正是工作单第二十八、二十九节想要的——"这是一个时代的
#: 转折"应当是结构上挣不到的。枚举值保留，因为模型必须能申报它，
#: 而申报正是触发 overclaim 标志的动作。
EVIDENCE_SCOPE_LADDER: tuple[tuple[ClaimScope, int, int], ...] = (
    (ClaimScope.SOCIETY, 6, 5),
    (ClaimScope.INDUSTRY, 3, 3),
    (ClaimScope.SEGMENT, 2, 2),
)

#: 正文里的全称量词。命中即判定"话说过头了"。
#:
#: 这份词表是新的（"不要另造词表"那条纪律是针对 `specificity` 的，那里复用
#: `features` 的既有词表）。命中词逐字回传，方便人核对误报。
BREADTH_MARKERS: tuple[str, ...] = (
    "整个",
    "所有",
    "全部",
    "任何",
    "每一个",
    "无一例外",
    "普遍",
    "全民",
    "一定",
    "必然",
    "注定",
    "永远",
    "从来",
    "彻底",
    "全面",
    "重新定义",
    "标志着",
    "宣告了",
    "终结了",
    "时代",
    "行业",
)

#: 无法核验的"原创性"表述（工作单第三十一节）。命中**不加分**，只作参考信号：
#: 本系统没有全网语义检索能力，声称"全网首发"是系统不能兑现的承诺。
ORIGINALITY_MARKERS: tuple[str, ...] = (
    "全网",
    "首创",
    "前所未有",
    "史上第一次",
    "从未有人",
    "第一个提出",
    "独家观点",
    "颠覆了",
)

#: 断言"谁出于什么目的做了什么"的词（工作单第六节）。
#:
#: 这类判断与全称量词不是一回事：`BREADTH_MARKERS` 管的是**范围**说大了
#: （"所有/整个行业"），这里管的是**读心**——公司的动机、人物的意图、资本
#: 的目的、发布的真实原因。它们在本系统里有一个共同性质：**材料几乎不可能
#: 直接证实**。一条帖子可以证实"发布了 9 款产品"，证实不了"他们是为了留个记录"。
#:
#: 命中不等于错。它触发的是那个**条件性下界**：没有 ≥2 条互相独立的来源时，
#: 这条判断只能写成"一种解读"，不能写成事实（`motive_unsupported`）。
MOTIVE_MARKERS: tuple[str, ...] = (
    "动机",
    "意图",
    "战略目的",
    "真实目的",
    "真实意图",
    "首要目标",
    "最主要的目的",
    "目的就是",
    "本意",
    "冲着",
    "本质上想要",
    "本质上就是",
    "实际上是在",
    "真正想要",
    "想证明",
    "为了证明",
    "为了掩盖",
    "为了洗白",
    "不打算",
    "并不打算",
    "根本没打算",
    "醉翁之意",
)

#: 允许一条动机判断达到"可以被接受"的下界：**≥2 条互相独立的场景**（来源与
#: 作者都要够，因为同一个人发的两条帖子不是两条独立证据）。
MIN_SOURCES_FOR_MOTIVE_CLAIM = 2

#: 对冲措辞：把这些判断写成"一种解读"而不是事实的说法。
#:
#: 复用 `features.HEDGES`（"可能/或许/一定程度上"）而不是另造一套，是因为
#: "有没有对冲"在两处必须是同一个判断。额外补的几条来自工作单第六节点名的
#: 降级写法："更像/一种解读/值得怀疑/从目前材料看"。
MOTIVE_HEDGES: tuple[str, ...] = features.HEDGES + (
    "更像",
    "看起来更像",
    "一种解读",
    "一种可能",
    "可以理解为",
    "值得怀疑",
    "从目前材料看",
    "从现有材料看",
    "未必",
    "不一定",
    "至少可以说",
)


def find_motive_markers(text: str) -> list[str]:
    """正文里出现的动机/意图断言，逐字回传（同 `find_breadth_markers`）。

    回传命中的词而不是布尔值：`"不打算"` 与 `"并不打算"` 同时命中同一条正文时，
    人要能一眼看出是哪一个触发的，才判得了误报。
    """
    if not isinstance(text, str):
        return []
    return [marker for marker in MOTIVE_MARKERS if marker in text]


def find_motive_hedges(text: str) -> list[str]:
    """正文里出现的对冲措辞。命中即认为这条动机判断**已经写成了解读**。"""
    if not isinstance(text, str):
        return []
    return [hedge for hedge in MOTIVE_HEDGES if hedge in text]

# --- 排序 -------------------------------------------------------------------

#: 前二名分差小于等于这个值就判为 ambiguous。
#:
#: 5.0 分是这么来的：总分是五个 0.15–0.30 权重的项在 0–100 上的加权和，所以
#: 单项动 0.25 就能让总分动 5 分以上——5 分小于单项的分辨率，属于"排序没有
#: 分出高下"。它同时也远小于结构上限的效果（50 分 vs 常见 70 分），
#: 所以并列不会被误认成封顶。
TIE_MARGIN = 5.0

#: 重复组判定的近似阈值（与 `app/style/spans.py` 同一个口径）。
NOVELTY_RESTATES_CONSENSUS = 0.25


@dataclass(frozen=True)
class EvidenceIndex:
    """打分要用到的、**已经解析好**的证据视图。

    `viewpoint.py` 从不读 state：它拿到的是一份解好的索引，正如
    `trends.score_topic` 拿到的是准备好的 `TopicScanItem`。这样这个模块
    对"state 里长什么样"零依赖，测试可以直接构造索引。

    字段一律用 `getattr` 鸭子类型取值（同 `app/style/recipe.py`），
    这样一份损坏的或太老的 checkpoint 会退化成空索引，而不是把一次运行变成 500。
    """

    #: 真实的 fact_id 集合。
    fact_ids: frozenset[str]
    #: fact_id → 它已核验的来源 id。
    fact_sources: Mapping[str, tuple[str, ...]]
    #: research_item.id → 作者名。**拿不到作者的条目不在表里**，不留空串占位。
    source_author: Mapping[str, str]
    #: 去重后的材料 id。
    material_ids: tuple[str, ...]
    #: 证据包声称引用过的材料 id（`pack.source_ids`，已由证据工具核验过）。
    referenced_material_ids: frozenset[str]
    #: 去掉纯链接材料之后的分母。
    countable_material_ids: tuple[str, ...]
    #: 每条材料对应的事实正文，供 novelty 的 n-gram 比对。
    fact_texts: tuple[str, ...]
    consensus: tuple[str, ...]
    counterpoints: tuple[str, ...]
    unknowns: tuple[str, ...]


@dataclass(frozen=True)
class ViewpointScore:
    """一个观点的全部评估中间量与结论。字段名与 `Angle`/`ViewpointAssessment`
    上的一一对应。"""

    claim: str
    evidence_support: float
    evidence_support_raw: float
    source_diversity: float
    counterpoint_awareness: float
    unknown_pressure: float
    specificity: float
    novelty: float
    newness_contribution: float
    confidence: float
    confidence_raw: float
    viewpoint_score: float
    viewpoint_score_raw: float
    bound_facts: int
    total_facts: int
    bound_sources: int
    bound_authors: int
    missing_author_sources: int
    declared_counterpoints: int
    bound_counterpoints: int
    total_materials: int
    unreferenced_materials: int
    breadth_markers: tuple[str, ...]
    originality_claim: bool
    restates_fact: bool
    duplicate_group: str
    evidence_scope: ClaimScope
    max_stance: ViewpointStance
    stance: ViewpointStance
    stance_declared: ViewpointStance | None
    claim_scope: ClaimScope | None
    scope_exceeds_evidence: bool
    scope_declared_missing: bool
    #: 正文里的动机/意图断言，逐字回传（工作单第六节）。带默认值：老 checkpoint、
    #: 手写 fixture 与本次改动之前的调用都不给这两个字段。
    motive_markers: tuple[str, ...] = field(default=())
    #: 断言的动机**没有被证据托住**（独立来源 < `MIN_SOURCES_FOR_MOTIVE_CLAIM`）。
    #: 命中时立场上界降到 cautious，并作为参考信号送进 Critic 的 accept 上界。
    motive_unsupported: bool = field(default=False)
    reasons: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class ViewpointSelection:
    """一次"选一个观点"的结论。"""

    index: int
    claim: str
    ambiguous: bool
    #: 前二名的分差。不足两个候选时为 None。
    margin: float | None
    #: 一句话说明，写进 tool summary 与 decision trace。
    how: str


# ---------------------------------------------------------------------------
# 索引构造
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def author_index(item_ids: Sequence[str], results: Sequence[Any]) -> dict[str, str]:
    """research_item.id → author_username。

    `zip` 与 `append_research` 维护的下标一一对应不变量一致（见
    `app/agent/tools/base.py::sources`）。

    **拿不到作者名的条目直接不进表**，不留空串、不留 "unknown" 占位：留占位会让
    所有未知作者被算成同一个人，把「3 位作者」静默变成「1 位」——这正是
    `trends.distinct_authors` 与 `discover._scan_items` 已经写下过的处置。
    也不抛异常：缺字段是数据问题，不是运行时问题。
    """
    table: dict[str, str] = {}
    for item_id, result in zip(item_ids, results):
        if not isinstance(item_id, str) or not item_id:
            continue
        author = getattr(result, "author_username", None)
        if isinstance(author, str) and author.strip():
            table[item_id] = author.strip()
    return table


def evidence_index(
    pack: Any, item_ids: Sequence[str] = (), results: Sequence[Any] = ()
) -> EvidenceIndex:
    """把一份证据包 + 本次材料解成打分用的索引。

    `pack` 允许是 None / dict / 任意对象（鸭子类型），解不出来就是一份空索引。
    """
    facts = getattr(pack, "facts", None) or ()
    fact_ids: set[str] = set()
    fact_sources: dict[str, tuple[str, ...]] = {}
    fact_texts: list[str] = []
    for fact in facts:
        fact_id = getattr(fact, "fact_id", "") or ""
        if not isinstance(fact_id, str) or not fact_id:
            continue
        fact_ids.add(fact_id)
        fact_sources[fact_id] = _names(getattr(fact, "source_ids", None))
        fact_texts.append(_text(getattr(fact, "fact", "")))

    # 材料 id 要去重：`create_research_items` 不按 platform_post_id 去重，
    # `append_research` 又是跨轮次拼接。重复的 id 会把分母灌大，
    # 从而**免费**压低 unknown_pressure。
    unique_ids = tuple(dict.fromkeys(i for i in item_ids if isinstance(i, str) and i))
    authors = author_index(item_ids, results)

    # 纯链接材料产出不了事实，把它算进"没产出任何事实"是在为采集器的
    # 工作不到位而惩罚观点。复用 trends 已有的正则，不另造一份。
    countable = tuple(
        item_id
        for item_id, result in zip(item_ids, results)
        if isinstance(item_id, str)
        and item_id
        and not LINK_ONLY_RE.match((_text(getattr(result, "content", "")) or "").strip())
    )

    return EvidenceIndex(
        fact_ids=frozenset(fact_ids),
        fact_sources=fact_sources,
        source_author=authors,
        material_ids=unique_ids,
        referenced_material_ids=frozenset(_names(getattr(pack, "source_ids", None))),
        countable_material_ids=tuple(dict.fromkeys(countable)),
        fact_texts=tuple(fact_texts),
        consensus=_names(getattr(pack, "consensus", None)),
        counterpoints=_names(getattr(pack, "counterpoints", None)),
        unknowns=_names(getattr(pack, "unknowns", None)),
    )


# ---------------------------------------------------------------------------
# 分项
# ---------------------------------------------------------------------------


def bound_fact_ids(claimed: Iterable[str], index: EvidenceIndex) -> list[str]:
    """声称引用的事实里**真的存在**的那些，去重且保序。

    去重是必需的：模型把 `f1` 写五遍不该让支撑度看起来是五条事实。
    """
    seen: dict[str, None] = {}
    for fact_id in claimed:
        if isinstance(fact_id, str) and fact_id in index.fact_ids:
            seen.setdefault(fact_id, None)
    return list(seen)


def bound_sources_of(fact_ids: Iterable[str], index: EvidenceIndex) -> list[str]:
    """这批事实覆盖到的来源 id（去重保序）。"""
    seen: dict[str, None] = {}
    for fact_id in fact_ids:
        for source_id in index.fact_sources.get(fact_id, ()):
            seen.setdefault(source_id, None)
    return list(seen)


def authors_of(source_ids: Iterable[str], index: EvidenceIndex) -> tuple[list[str], int]:
    """来源对应的作者集合，以及**没有作者名**的来源条数。

    未知作者不进集合（同 `author_index` 的理由），但要把条数报出来——
    否则"3 条来源只有 1 位作者"与"3 条来源里 2 条没作者信息"看起来一样。
    """
    authors: dict[str, None] = {}
    missing = 0
    for source_id in source_ids:
        author = index.source_author.get(source_id)
        if author:
            authors.setdefault(author, None)
        else:
            missing += 1
    return list(authors), missing


def score_evidence_support(
    *, bound_facts: int, total_facts: int, bound_sources: int
) -> tuple[float, float]:
    """支撑度，返回 (封顶后, 封顶前)。

    `total_facts > 0` 由调用方保证（`generate_angles` 的前置条件是有证据）。
    """
    facts_term = min(1.0, bound_facts / EVIDENCE_SUPPORT_SATURATION_FACTS)
    coverage_term = bound_facts / total_facts if total_facts > 0 else 0.0
    raw = (
        EVIDENCE_SUPPORT_FACTS_WEIGHT * facts_term
        + (1.0 - EVIDENCE_SUPPORT_FACTS_WEIGHT) * coverage_term
    )
    if bound_sources <= 1 and bound_facts >= 1:
        return round(min(raw, SINGLE_SOURCE_EVIDENCE_CAP), 4), round(raw, 4)
    return round(raw, 4), round(raw, 4)


def score_source_diversity(
    *, bound_sources: int, bound_authors: int, missing_author_sources: int
) -> float:
    """来源与作者的多样性。

    一条作者信息都没有时**不是**退化成"只看来源数"——那会让无作者来源成为
    通往高分的捷径。而是把来源数那一项压到 `UNKNOWN_AUTHOR_DIVERSITY_CAP` 以下。
    """
    sources_term = min(1.0, bound_sources / SOURCE_DIVERSITY_FULL_SOURCES)
    if bound_sources > 0 and missing_author_sources == bound_sources:
        return round(min(sources_term, UNKNOWN_AUTHOR_DIVERSITY_CAP), 4)
    authors_term = min(1.0, bound_authors / SPREAD_FULL_AUTHORS)
    return round(
        SOURCE_DIVERSITY_AUTHOR_WEIGHT * authors_term
        + (1.0 - SOURCE_DIVERSITY_AUTHOR_WEIGHT) * sources_term,
        4,
    )


def declared_stance_of(draft: Any) -> ViewpointStance | None:
    """取模型**申报**的立场——注意是申报值，不是生效值。

    两个调用方喂进来的东西形状不同，而它们各自的"申报"藏在不同的字段上：

    - `AngleDraft.stance` **就是**申报（草稿上没有任何算出来的东西）；
    - `Angle.stance` 是**钳过之后生效的值**，模型原本申报的那个留在
      `Angle.stance_declared` 上。

    所以按**字段在不在**分流，不按真值分流：`Angle` 上 `stance_declared`
    确实可能是 `None`（模型压根没申报），而"没申报"与"申报了上界"是两件
    必须分得开的事——用 `or` 回退会把后者当成前者。

    不这么分的代价是打分函数**对自己的输出不幂等**：`select_angle` 会拿
    `Angle` 再打一遍分，那时 `Angle.stance`（例如已被证据从 strong 钳到
    moderate）会被读成"模型申报了 moderate"，复算出的 `stance_declared`
    就凭空多了一个值。值本身这一轮没歪，但它让"这个数能不能原样算出来"
    这个问题失去了答案——而那正是 RULE-023 要问的。
    """
    if hasattr(draft, "stance_declared"):
        declared = getattr(draft, "stance_declared", None)
    else:
        declared = getattr(draft, "stance", None)
    return declared if isinstance(declared, ViewpointStance) else None


def score_counterpoint_awareness(*, declared: int, bound: int) -> float:
    """反方意识：绑定到事实的反方记满分，只是写了一句的记三分之一。

    `declared` 由调用方只统计**非空**的条目——空字符串不该换到"声明过"的分。
    """
    unbound = max(0, declared - bound)
    raw = COUNTERPOINT_BOUND_CREDIT * bound + COUNTERPOINT_DECLARED_CREDIT * unbound
    ceiling = COUNTERPOINT_FULL_COUNT * COUNTERPOINT_BOUND_CREDIT
    return round(min(1.0, raw / ceiling), 4) if ceiling > 0 else 0.0


def score_unknown_pressure(index: EvidenceIndex) -> tuple[float, int, int]:
    """未知压力，返回 (值, 未覆盖材料数, 参与计算的材料数)。

    **这一项不由观点阶段的模型自报决定**，这是工作单第十一节的关键要求：
    `research_item_ids` 由 discover/research 定下，`pack.source_ids` 由
    `_keep_traceable` 在 `generate_angles` 之前定下，所以观点阶段无论怎么写、
    写多少未知，都动不了这一项。

    取 `max(未覆盖率, 自报未知的压力)` 而不是加权和：加权和允许模型通过
    **少写未知**来把压力调低（少写一段就少一份权重），而 max 意味着
    自报项只能把压力**顶上去**，永远不能把外生的那一项按下来。

    没有任何可计材料时不算 0，而是退化成只看自报项，并在理由里说明——
    "算不出来"与"算出来是零"必须分得开。
    """
    countable = index.countable_material_ids
    uncovered: float | None = None
    unreferenced = 0
    if countable:
        unreferenced = sum(
            1 for item_id in countable if item_id not in index.referenced_material_ids
        )
        uncovered = unreferenced / len(countable)

    declared = min(1.0, len(index.unknowns) / UNKNOWN_SATURATION)
    if uncovered is None:
        return round(UNKNOWN_COUNT_WEIGHT * declared, 4), 0, 0
    return round(max(uncovered, UNKNOWN_COUNT_WEIGHT * declared), 4), unreferenced, len(
        countable
    )


def score_specificity(claim: str) -> float:
    """具体的痕迹：专有实体与数字，减去套话与模糊限定词。

    只读 `claim`，**不读 `reasoning`**：问的是"这个判断本身具体吗"。
    把解释里的实体算进来，会让一个空泛的判断借它自己的论证拿到具体性——
    而且在推理里引用事实是好习惯，不该被当成具体性来奖励。

    这是**表面代理**，不是"具体性"本身（同 `features` 里那些自我声明为
    近似量的原语）。数字要从实体里减去，否则一个 "2026" 会被两处各算一次。
    """
    text = claim or ""
    entities = [
        e
        for e in features.find_concrete_entities(text)
        if e and not e[:1].isdigit()
    ]
    numbers = features.count_numbers(text)
    generic = features.find_generic_phrases(text)
    hedges = features.count_hedges(text)

    positive = (
        0.70 * min(1.0, len(entities) / SPECIFICITY_FULL_ENTITIES)
        + 0.30 * min(1.0, numbers / SPECIFICITY_FULL_NUMBERS)
    )
    penalty = GENERIC_PHRASE_PENALTY * len(generic) + HEDGE_PENALTY * hedges
    return round(max(0.0, min(1.0, positive - penalty)), 4)


def find_breadth_markers(text: str) -> list[str]:
    """正文里命中的全称量词，逐字回传以便人核对误报。

    `一定` 会落在 `一定程度上` 这类模糊限定词内部，那是一个**诚实**的措辞
    而不是全称判断。所以逐词减去被 `features.HEDGES` 包住的次数——复用已有的
    模糊词表，而不是另造一份例外表，否则两处会各自漂移。
    """
    body = text or ""
    hits: list[str] = []
    for marker in BREADTH_MARKERS:
        count = body.count(marker) - sum(
            body.count(hedge) for hedge in features.HEDGES if marker in hedge
        )
        if count > 0:
            hits.append(marker)
    return hits


def find_originality_markers(text: str) -> list[str]:
    """无法核验的"原创性"表述（工作单第三十一节）。"""
    body = text or ""
    return [marker for marker in ORIGINALITY_MARKERS if marker in body]


def evidence_scope_for(
    *, bound_facts: int, bound_sources: int, bound_authors: int
) -> ClaimScope:
    """证据能支撑到哪个范围，取阶梯上能过的最高一档。"""
    if bound_facts <= 1 and bound_sources <= 1:
        return ClaimScope.FACT
    for scope, min_sources, min_authors in EVIDENCE_SCOPE_LADDER:
        if bound_sources >= min_sources and bound_authors >= min_authors:
            return scope
    return ClaimScope.CASE


def score_novelty(
    *,
    claim: str,
    index: EvidenceIndex,
    sibling_claims: Sequence[str],
    declared_restatement: bool,
) -> tuple[float, bool, str]:
    """新颖性，返回 (值, 是否只是复述事实, 重复组名)。

    **能算的只是"不是复述、不是重复"**，不是"全世界没人说过"——本系统没有
    全网语义检索能力，工作单第三十一节禁止那样声称。

    分级刻意做得粗（四档），因为细粒度在这里是假精度。模型只能通过
    `declared_restatement` 把这一项**调低**，永远调不高。
    """
    body = (claim or "").strip()
    if not body:
        return 0.0, False, ""

    for text in index.fact_texts:
        if overlapping_spans(body, corpus_spans([text])):
            return 0.0, True, ""

    for line in index.consensus:
        if overlapping_spans(body, corpus_spans([line])):
            return NOVELTY_RESTATES_CONSENSUS, False, ""

    for other in sibling_claims:
        other_body = (other or "").strip()
        if not other_body or other_body == body:
            continue
        if overlapping_spans(body, corpus_spans([other_body])):
            # 两个都记同一个组名（取字典序小的那个 claim，与 id 无关，
            # 所以在 id 重排后仍然稳定）。不"保一个杀一个"——那需要一个
            # 与新颖性无关的预排序，第一版不做，代价是成对的一起被压。
            return 0.0, False, min(body, other_body)

    if declared_restatement:
        return 0.0, False, ""

    return 1.0, False, ""


def max_stance_for(
    *,
    confidence: float,
    bound_sources: int,
    bound_authors: int,
    counterpoint_awareness: float,
    unknown_pressure: float,
    scope_exceeds_evidence: bool,
    scope_gap: int,
    motive_unsupported: bool = False,
) -> tuple[ViewpointStance, list[str]]:
    """证据允许的立场上界，以及为什么。

    工作单第九、十节要求"允许 cautious / no_strong_viewpoint"必须是一条
    **策略**，不能取决于模型当天的心情。所以上界由证据推出，模型只能在
    上界之内选择更保守的表达，不能更笃定。

    `motive_unsupported` 是一条**独立的**下界（工作单第六节）：断言的动机没有
    独立来源托住时，上界直接降到 cautious。它与"范围超过证据"共用同一档
    （cautious）而不是新增一档枚举值——枚举值每加一个，下游所有按档位分支的
    地方都要多判一次，而这里要表达的仍然是同一件事："这条判断说得比材料笃定"。
    """
    reasons: list[str] = []
    if (
        confidence >= STRONG_MIN_CONFIDENCE
        and bound_sources >= MIN_SOURCES_FOR_STRONG
        and bound_authors >= MIN_AUTHORS_FOR_STRONG
        and counterpoint_awareness > 0
        and unknown_pressure <= UNKNOWN_PRESSURE_CEILING_FOR_MODERATE
    ):
        ceiling = ViewpointStance.STRONG
        reasons.append(
            f"confidence {confidence:.2f} ≥ {STRONG_MIN_CONFIDENCE}、"
            f"绑定 {bound_sources} 条来源 / {bound_authors} 位作者、已回应反方，"
            f"上界为 strong"
        )
    elif (
        confidence >= MODERATE_MIN_CONFIDENCE
        and bound_sources >= MIN_SOURCES_FOR_STRONG
        and unknown_pressure <= UNKNOWN_PRESSURE_CEILING_FOR_MODERATE
    ):
        ceiling = ViewpointStance.MODERATE
        reasons.append(
            f"confidence {confidence:.2f} ≥ {MODERATE_MIN_CONFIDENCE} 且"
            f"绑定 {bound_sources} 条来源，上界为 moderate"
        )
    elif confidence >= CAUTIOUS_MIN_CONFIDENCE:
        ceiling = ViewpointStance.CAUTIOUS
        reasons.append(
            f"confidence {confidence:.2f} ≥ {CAUTIOUS_MIN_CONFIDENCE}，上界为 cautious"
        )
    else:
        ceiling = ViewpointStance.UNCERTAIN
        reasons.append(
            f"confidence {confidence:.2f} < {CAUTIOUS_MIN_CONFIDENCE}，上界为 uncertain"
        )

    if scope_exceeds_evidence:
        cap = (
            ViewpointStance.CAUTIOUS if scope_gap >= 2 else ViewpointStance.MODERATE
        )
        if _STANCE_INDEX[cap] < _STANCE_INDEX[ceiling]:
            ceiling = cap
            reasons.append(
                f"主张范围超过证据范围 {scope_gap} 档，上界下调为 {cap.value}"
            )

    if motive_unsupported and _STANCE_INDEX[ViewpointStance.CAUTIOUS] < _STANCE_INDEX[ceiling]:
        ceiling = ViewpointStance.CAUTIOUS
        reasons.append(
            f"判断在说动机/意图，而独立来源不足 {MIN_SOURCES_FOR_MOTIVE_CLAIM} 条，"
            f"上界下调为 cautious（只能写成一种解读，不能写成事实）"
        )
    return ceiling, reasons


# ---------------------------------------------------------------------------
# Critic 的 accept 上界
# ---------------------------------------------------------------------------


def accept_ceiling_for(
    *,
    content: str,
    claim: str,
    bound_sources: int,
    bound_authors: int,
    stance: ViewpointStance | None,
    stance_declared: ViewpointStance | None,
    max_stance: ViewpointStance | None,
) -> tuple[bool, str]:
    """这条正文现在能不能被 accept。返回 `(能不能, 不能的理由)`。

    工作单第七节把这条写成硬要求：**观点强度 ≤ 证据强度**。而在此之前，
    `accept` 是 Critic 的 LLM 自由选择——`viewpoint.py` 算出的上界、单一来源
    上限、范围超出证据，全都只是渲染给模型看的**参考事实**。模型说 accept，
    流程就走 accept。

    这里不改 Critic 的评分，只给"结论"加一个确定性的上界，与 `max_stance_for`
    对立场做的事完全一样：模型可以在上界之内更保守，不能更笃定。

    两条判据，都要求正文**没有**对冲措辞：

    1. **动机判断没有独立来源托住**（`MIN_SOURCES_FOR_MOTIVE_CLAIM`）。
       材料证实得了"发布了 9 款产品"，证实不了"他们是为了留个记录"。
    2. **申报的立场强于证据允许的上界**。`stance` 已经被钳到上界了，但
       `stance_declared` 记着模型原本想要的那一档——它比上界强，说明这条判断
       是按比证据更笃定的调子写出来的。

    为什么要求"没有对冲"：这两条判据都能靠**改写成解读**来满足（"更像""可能"
    "一种解读"），也就是工作单第五节要求的那种降级。不要求对冲的话，一条已经
    写成"更像留个记录"的稿子会被永远改判成 revise——改稿预算空转两轮，最后
    交付的还是同一句话。判的是**写出来的东西**，不是模型当初想说什么。

    判据里没有"分数低"这种含糊的项：两条都用量得出的量（独立来源数、两档
    立场的先后、逐字回传的对冲词）。
    """
    independent = min(bound_sources, bound_authors)
    markers = find_motive_markers(content) or find_motive_markers(claim)
    if markers and independent < MIN_SOURCES_FOR_MOTIVE_CLAIM and not find_motive_hedges(content):
        return False, (
            f"正文在断言动机/意图（{'、'.join(markers)}），而互相独立的来源只有 "
            f"{independent} 条（来源 {bound_sources} / 作者 {bound_authors}）——"
            f"把'他们是为了……'写成了事实。改写方向：加对冲（更像 / 可能 / "
            f"一种解读 / 从目前材料看），或者补第二个独立来源"
        )

    if (
        stance_declared is not None
        and max_stance is not None
        and _STANCE_INDEX[stance_declared] > _STANCE_INDEX[max_stance]
        and not find_motive_hedges(content)
    ):
        return False, (
            f"这条判断原本按 {stance_declared.value} 的强度申报，而证据只支持到 "
            f"{max_stance.value}（生效立场 {stance.value if stance else '未定'}）——"
            f"正文把结论说得比证据笃定。改写方向：按 {max_stance.value} 的口径重写，"
            f"或者补足材料让上界抬起来"
        )

    return True, ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def score_viewpoints(
    drafts: Sequence[Any],
    index: EvidenceIndex,
    *,
    total_facts: int | None = None,
) -> list[ViewpointScore]:
    """给一批观点草稿打确定性分。本模块唯一的对外入口。

    `drafts` 是任意带 `claim` / `evidence_ids` / `counterpoints` /
    `counterpoint_evidence_ids` / `claim_scope` / `stance` /
    `restates_known_material` 属性的对象（鸭子类型）。

    一次算一批而不是逐个算：`novelty` 要看到**同批其他观点**才知道有没有
    互相重复。这也让"同一份输入 → 同一组结果"这条性质覆盖到批内比较。
    """
    claims = [(getattr(d, "claim", "") or "").strip() for d in drafts]
    resolved_total = (
        total_facts if total_facts is not None else len(index.fact_ids)
    )
    unknown_pressure, unreferenced, countable_total = score_unknown_pressure(index)

    results: list[ViewpointScore] = []
    for position, draft in enumerate(drafts):
        claim = claims[position]
        reasons: list[str] = []

        facts = bound_fact_ids(getattr(draft, "evidence_ids", None) or (), index)
        sources = bound_sources_of(facts, index)
        authors, missing_authors = authors_of(sources, index)

        evidence_support, evidence_support_raw = score_evidence_support(
            bound_facts=len(facts), total_facts=resolved_total, bound_sources=len(sources)
        )
        reasons.append(
            f"绑定 {len(facts)} 条事实（证据包共 {resolved_total} 条）、"
            f"覆盖 {len(sources)} 条来源 / {len(authors)} 位作者，"
            f"evidence_support {evidence_support:.4f}"
        )
        if evidence_support_raw != evidence_support:
            reasons.append(
                f"单一来源上限 {SINGLE_SOURCE_EVIDENCE_CAP:g} 生效"
                f"（封顶前 {evidence_support_raw:.4f}）"
            )
        if missing_authors:
            reasons.append(f"{missing_authors} 条来源没有作者信息，未计入作者数")

        source_diversity = score_source_diversity(
            bound_sources=len(sources),
            bound_authors=len(authors),
            missing_author_sources=missing_authors,
        )
        reasons.append(
            f"来源多样性 {source_diversity:.4f}"
            f"（满值 {SOURCE_DIVERSITY_FULL_SOURCES} 条来源 / "
            f"{SPREAD_FULL_AUTHORS} 位作者）"
        )

        declared_points = [
            p for p in (getattr(draft, "counterpoints", None) or ()) if _text(p).strip()
        ]
        bound_points = bound_fact_ids(
            getattr(draft, "counterpoint_evidence_ids", None) or (), index
        )
        counterpoint_awareness = score_counterpoint_awareness(
            declared=len(declared_points), bound=len(bound_points)
        )
        if declared_points or bound_points:
            reasons.append(
                f"反方 {len(declared_points)} 条、其中 {len(bound_points)} 条绑定到事实，"
                f"counterpoint_awareness {counterpoint_awareness:.4f}"
            )
        elif index.counterpoints:
            reasons.append(
                f"证据包里有 {len(index.counterpoints)} 条反方说法，"
                f"这个观点一条都没有回应，counterpoint_awareness 记 0"
            )
        else:
            reasons.append("证据包里没有反方说法，这个观点也没有自己构造一个，记 0")

        reasons.append(
            f"{countable_total} 条可计材料里 {unreferenced} 条没有被任何事实引用，"
            f"unknown_pressure {unknown_pressure:.4f}"
            if countable_total
            else "本次没有可计的材料清单，未知压力只看证据包自报的未知项"
        )

        specificity = score_specificity(claim)
        reasons.append(f"claim 的具体痕迹 specificity {specificity:.4f}")

        novelty, restates_fact, duplicate_group = score_novelty(
            claim=claim,
            index=index,
            sibling_claims=[c for i, c in enumerate(claims) if i != position],
            declared_restatement=bool(
                getattr(draft, "restates_known_material", False)
            ),
        )
        newness_contribution = round(
            novelty * min(1.0, evidence_support / NOVELTY_GATE_FULL_EVIDENCE), 4
        )
        reasons.append(
            f"novelty {novelty:.4f}，经证据门控后 newness_contribution "
            f"{newness_contribution:.4f}（门控满值 {NOVELTY_GATE_FULL_EVIDENCE}）"
        )
        if duplicate_group:
            reasons.append("与同批另一个观点有 12 字以上重合，两者都记 0")
        if restates_fact:
            reasons.append("claim 与某条事实有 12 字以上重合，判定为事实复述")

        breadth_markers = find_breadth_markers(claim)
        originality_claim = bool(find_originality_markers(claim))
        if originality_claim:
            reasons.append(
                "claim 用了「全网/首创」类无法核验的表述；本系统没有全网检索能力，"
                "这类表述不作为新颖性加分，也不应写进正文"
            )

        evidence_scope = evidence_scope_for(
            bound_facts=len(facts), bound_sources=len(sources), bound_authors=len(authors)
        )
        declared_scope = getattr(draft, "claim_scope", None)
        if not isinstance(declared_scope, ClaimScope):
            declared_scope = None
        scope_declared_missing = declared_scope is None

        scope_gap = 0
        if declared_scope is not None:
            scope_gap = max(
                0,
                CLAIM_SCOPE_BREADTH[declared_scope] - CLAIM_SCOPE_BREADTH[evidence_scope],
            )
        overbroad_prose = bool(breadth_markers) and (
            CLAIM_SCOPE_BREADTH[evidence_scope] <= CLAIM_SCOPE_BREADTH[ClaimScope.CASE]
        )
        scope_exceeds_evidence = scope_gap > 0 or overbroad_prose
        if declared_scope is None:
            reasons.append("模型没有申报 claim_scope")
        elif scope_gap > 0:
            reasons.append(
                f"申报范围 {declared_scope.value} 超过证据能支撑的 "
                f"{evidence_scope.value}（差 {scope_gap} 档）"
            )
        if overbroad_prose:
            reasons.append(
                f"证据范围只有 {evidence_scope.value}，但正文出现全称量词："
                f"{'、'.join(breadth_markers)}"
            )

        # 动机/意图断言（工作单第六节）。判它需要**独立**来源，所以取来源数与
        # 作者数的较小值：同一个人发的两条帖子不是两条互相独立的证据，而
        # "他们是为了……"这种判断最容易被同一个人反复说的话托住。
        motive_markers = find_motive_markers(claim)
        independent_sources = min(len(sources), len(authors))
        motive_unsupported = (
            bool(motive_markers)
            and independent_sources < MIN_SOURCES_FOR_MOTIVE_CLAIM
        )
        if motive_unsupported:
            reasons.append(
                f"判断在断言动机/意图（{'、'.join(motive_markers)}），"
                f"而互相独立的来源只有 {independent_sources} 条"
                f"（作者 {len(authors)} 位 / 来源 {len(sources)} 条）；"
                f"材料证实得了「做了什么」，证实不了「为了什么」——"
                f"只能写成一种解读"
            )

        confidence_raw = sum(
            weight
            * {
                "evidence_support": evidence_support,
                "source_diversity": source_diversity,
                "counterpoint_awareness": counterpoint_awareness,
                "unknown_certainty": 1.0 - unknown_pressure,
            }[name]
            for name, weight in CONFIDENCE_WEIGHTS
        )
        confidence = confidence_raw
        if len(sources) <= 1:
            confidence = min(confidence, SINGLE_SOURCE_CONFIDENCE_CAP)
        if counterpoint_awareness <= 0:
            confidence = min(confidence, NO_COUNTERPOINT_CONFIDENCE_CAP)
        confidence = round(confidence, 4)
        confidence_raw = round(confidence_raw, 4)
        formula = " + ".join(f"{name}×{weight:g}" for name, weight in CONFIDENCE_WEIGHTS)
        reasons.append(
            f"confidence_raw = {formula} = {confidence_raw:.4f}"
        )
        if confidence != confidence_raw:
            reasons.append(f"上限生效（单一来源 / 无反方），confidence {confidence:.4f}")

        max_stance, stance_reasons = max_stance_for(
            confidence=confidence,
            bound_sources=len(sources),
            bound_authors=len(authors),
            counterpoint_awareness=counterpoint_awareness,
            unknown_pressure=unknown_pressure,
            scope_exceeds_evidence=scope_exceeds_evidence,
            scope_gap=scope_gap,
            motive_unsupported=motive_unsupported,
        )
        reasons.extend(stance_reasons)

        declared_stance = declared_stance_of(draft)
        if declared_stance is None:
            stance = max_stance
            reasons.append(
                f"模型未声明立场，取证据允许的上界 {max_stance.value}"
                "（上界本身由证据推出，不构成超证据承诺）"
            )
        elif _STANCE_INDEX[declared_stance] > _STANCE_INDEX[max_stance]:
            stance = max_stance
            reasons.append(
                f"模型声明 stance={declared_stance.value}，但证据只支持到 "
                f"{max_stance.value}，已下调"
            )
        else:
            stance = declared_stance

        viewpoint_score_raw = 100.0 * (
            VIEWPOINT_WEIGHTS[0][1] * evidence_support
            + VIEWPOINT_WEIGHTS[1][1] * source_diversity
            + VIEWPOINT_WEIGHTS[2][1] * specificity
            + VIEWPOINT_WEIGHTS[3][1] * newness_contribution
            + VIEWPOINT_WEIGHTS[4][1] * counterpoint_awareness
        )
        viewpoint_score = viewpoint_score_raw
        if evidence_support < MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE:
            viewpoint_score = min(viewpoint_score, LOW_SUPPORT_VIEWPOINT_CAP)
            reasons.append(
                f"支撑度 {evidence_support:.4f} < {MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE}，"
                f"观点分压到 {LOW_SUPPORT_VIEWPOINT_CAP:g} 以下"
            )
        score_formula = " + ".join(
            f"{name}×{weight:g}" for name, weight in VIEWPOINT_WEIGHTS
        )
        reasons.append(
            f"viewpoint_score_raw = 100 × ({score_formula}) = {viewpoint_score_raw:.2f}"
        )

        results.append(
            ViewpointScore(
                claim=claim,
                evidence_support=evidence_support,
                evidence_support_raw=evidence_support_raw,
                source_diversity=source_diversity,
                counterpoint_awareness=counterpoint_awareness,
                unknown_pressure=unknown_pressure,
                specificity=specificity,
                novelty=novelty,
                newness_contribution=newness_contribution,
                confidence=confidence,
                confidence_raw=confidence_raw,
                viewpoint_score=round(viewpoint_score, 2),
                viewpoint_score_raw=round(viewpoint_score_raw, 2),
                bound_facts=len(facts),
                total_facts=resolved_total,
                bound_sources=len(sources),
                bound_authors=len(authors),
                missing_author_sources=missing_authors,
                declared_counterpoints=len(declared_points),
                bound_counterpoints=len(bound_points),
                total_materials=countable_total,
                unreferenced_materials=unreferenced,
                breadth_markers=tuple(breadth_markers),
                motive_markers=tuple(motive_markers),
                motive_unsupported=motive_unsupported,
                originality_claim=originality_claim,
                restates_fact=restates_fact,
                duplicate_group=duplicate_group,
                evidence_scope=evidence_scope,
                max_stance=max_stance,
                stance=stance,
                stance_declared=declared_stance,
                claim_scope=declared_scope,
                scope_exceeds_evidence=scope_exceeds_evidence,
                scope_declared_missing=scope_declared_missing,
                reasons=tuple(reasons),
            )
        )
    return results


def rank_key(score: ViewpointScore) -> tuple:
    """排序键：观点分降序，其次置信度降序，再次绑定来源数降序，末项用 claim。

    末项不用"保持原顺序"的理由与 `trends.rank_key` 相同：LLM 给出的角度顺序
    对同一份证据并不稳定，而分数、置信度、来源数都相同时的先后对下游没有意义——
    用一个确定的字符串收尾，至少让同一份输入永远排出同一个顺序。
    """
    return (
        -score.viewpoint_score,
        -score.confidence,
        -score.bound_sources,
        score.claim,
    )


def select_viewpoint(
    scores: Sequence[ViewpointScore], *, target: str = "", margin: float = TIE_MARGIN
) -> ViewpointSelection | None:
    """在已打分的观点里选定一个，返回 None 表示没有候选。

    与 `tools/discover.py::select_topic` 同一套做法：**不调用 LLM**。
    分数已经算好并排好序，让模型再"选一次"只会给它一次推翻确定性排序的机会，
    而 trace 里记下的"为什么选它"就变成了模型的自述而不是分数（工作单第二十节）。

    与 `select_topic` 的差别是**容差**（工作单第二十一节）：前二名分差在
    `margin` 以内时判为 ambiguous，此时 Planner 的 `target` 只要落在并列区间内
    就被采纳——因为那个区间里"哪个第一"本来就没有分出高下。超出区间的 target
    会被忽略，并把差了多少分写进 `how`。

    **ambiguous 不会让结果悬空**：流程需要一个观点才能往下走，所以总会选一个；
    变的只是"这次是在并列区间里选的"这件事被记了下来。
    """
    if not scores:
        return None
    ordered = sorted(range(len(scores)), key=lambda i: rank_key(scores[i]))
    top_index = ordered[0]
    top = scores[top_index]

    second = scores[ordered[1]] if len(ordered) > 1 else None
    gap = top.viewpoint_score - second.viewpoint_score if second else None
    ambiguous = gap is not None and gap <= margin

    how = f"按确定性排序选定 {top.claim[:20]}（观点分 {top.viewpoint_score:.1f}）"
    if ambiguous and gap is not None:
        how += f"；前二名只差 {gap:.1f} 分（容差 {margin:g}），属于并列区间"

    wanted = (target or "").strip()
    if wanted:
        match = next((i for i, s in enumerate(scores) if s.claim.strip() == wanted), None)
        if match is None:
            how += f"；决策给的「{wanted}」不是有效观点，按排序选取"
        elif match == top_index:
            how = f"决策选定 {top.claim[:20]}，与确定性排序一致"
            if ambiguous and gap is not None:
                how += f"（前二名只差 {gap:.1f} 分）"
        else:
            deficit = top.viewpoint_score - scores[match].viewpoint_score
            if deficit <= margin:
                top_index, top = match, scores[match]
                how = (
                    f"决策选定 {top.claim[:20]}，比最高分低 {deficit:.1f} 分，"
                    f"在容差 {margin:g} 分内，采纳"
                )
            else:
                how += (
                    f"；决策给的「{wanted[:20]}」比最高分低 {deficit:.1f} 分"
                    f"（超过容差 {margin:g}），按确定性排序选定"
                )

    return ViewpointSelection(
        index=top_index,
        claim=top.claim,
        ambiguous=ambiguous,
        margin=round(gap, 2) if gap is not None else None,
        how=how,
    )


__all__ = [
    "EvidenceIndex",
    "ViewpointScore",
    "ViewpointSelection",
    "accept_ceiling_for",
    "author_index",
    "bound_fact_ids",
    "bound_sources_of",
    "authors_of",
    "evidence_index",
    "evidence_scope_for",
    "find_breadth_markers",
    "find_motive_hedges",
    "find_motive_markers",
    "find_originality_markers",
    "max_stance_for",
    "rank_key",
    "score_counterpoint_awareness",
    "score_evidence_support",
    "score_novelty",
    "score_source_diversity",
    "score_specificity",
    "score_unknown_pressure",
    "score_viewpoints",
    "select_viewpoint",
]
