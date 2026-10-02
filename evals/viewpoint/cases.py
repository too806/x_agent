"""观点评估的**手写**正反例（工作单第三十二节）。

七类，一类一组数据。全部合成、不碰真实 X、不碰真实语料、不碰 LLM——
所以这份基准在 CI 里跑不需要网络，也不会有"今天红了明天绿了"的抖动。

## 预期是数据，不是注释

每组数据带一份 `expect`：一串 `(草稿下标, 字段, 关系, 期望值)` 断言。
`selftest.py` 逐条核对这些断言，**不掺任何人工判断**。写成数据而不是写在
检查函数的注释里，是为了两件事：

1. "这一组应当通过 / 应当被判出问题"在报告里是**可读的**，不藏在代码里；
2. 关系是显式的（`<= 0.40` 而不是"被压住了"），所以哪天常数被改动，
   失败信息会直接指向"0.45 <= 0.40 不成立"，而不是一句"不符合预期"。

## 每组都成对

只看正例的基准分不清"检测器在判"与"检测器对什么都判"。所以每一类里都放了
一条**长得很像但不该被认出**的对照数据：

- `strong_evidence` 里没有任何一项被压住，`overclaim` 里同一批证据被同一个
  判断写成全称句——两者的差别只在**申报的范围**；
- `single_source` 有三条事实但只有一条来源，`strong_evidence` 有同样的
  事实数但四条来源；
- `conflicting_evidence` / `counterpoint_present` / `prediction` / `overclaim`
  各给两条草稿，一条"做对了"、一条"做错了"，断言的是一个**差**。

选择器与关系词（`selftest.py` 里实现）：

    "=="、"!="、"<"、"<="、">"、">="  —— 普通比较
    "is"                              —— 恒等（枚举必须用这个，不能用 ==）
    "contains"                        —— 序列包含某个元素
    "empty" / "nonempty"             —— 序列为空 / 非空
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.schemas import (
    AngleDraft,
    ClaimScope,
    EvidenceFact,
    EvidencePack,
    ViewpointStance,
)


@dataclass(frozen=True)
class Material:
    """一条研究材料的最小替身：打分只读这两个字段。

    真机上传进来的研究结果是 ORM 对象，这里的替身只要**长得像**就够了——
    `app/agent/viewpoint.py` 一律用 `getattr` 取值（同 `app/style/recipe.py`）。
    """

    author_username: str
    content: str


@dataclass(frozen=True)
class Case:
    """一组数据，以及它必须满足的断言。"""

    category: str
    note: str
    pack: EvidencePack
    materials: dict[str, Material]
    #: 打分器的输入，类型是 **`AngleDraft`** 而不是 `Angle`：这些是"模型声明了
    #: 什么"，不是"存下来的是什么"。两者的区别在立场那一项上是实质性的——
    #: `AngleDraft.stance` 是**申报值**，`Angle.stance` 是**被证据上界钳过的
    #: 生效值**（申报值另存 `stance_declared`）。用 `Angle` 造草稿会得到一份
    #: 自相矛盾的记录（存储模型却只带申报），而 `viewpoint.declared_stance_of`
    #: 按字段**存在与否**分派，它会读成"模型没申报"——见那个函数的说明。
    drafts: list[AngleDraft]
    #: `(草稿下标, 字段, 关系, 期望值)`。
    expect: list[tuple[int, str, str, Any]] = field(default_factory=list)


def make_pack(facts, *, consensus=(), counterpoints=(), unknowns=(), sources=()) -> EvidencePack:
    return EvidencePack(
        facts=[
            EvidenceFact(fact_id=fid, fact=text, source_ids=list(src))
            for fid, text, src in facts
        ],
        consensus=list(consensus),
        counterpoints=list(counterpoints),
        unknowns=list(unknowns),
        source_ids=list(sources) or sorted({s for _, _, src in facts for s in src}),
    )


def make_materials(**kwargs: str) -> dict[str, Material]:
    """`s1="alice"` → `{"s1": Material("alice", "正文：s1")}`。"""
    return {sid: Material(author, f"正文：{sid}") for sid, author in kwargs.items()}


# ---------------------------------------------------------------------------
# 一、证据充分：什么都不该被压住
# ---------------------------------------------------------------------------

STRONG = Case(
    category="strong_evidence",
    note="四条事实、四条独立来源、四位作者，反方绑到了事实；结论应当能到 strong",
    pack=make_pack(
        [
            ("f1", "OpenAI 在 2026 年 9 月把 API 价格下调了四成", ["s1"]),
            ("f2", "Anthropic 在同一周下调了批量推理的价格", ["s2"]),
            ("f3", "两家都把企业版的最低档位降了价", ["s3"]),
            ("f4", "两家都没有调整个人订阅的价格", ["s4"]),
        ],
        consensus=["厂商在争夺企业客户"],
        counterpoints=["降价可能只是季度促销"],
        unknowns=["是否长期"],
    ),
    materials=make_materials(s1="alice", s2="bob", s3="carol", s4="dave"),
    drafts=[
        AngleDraft(
            claim="GPT-5 与 Claude 的企业版定价在 2026 年 9 月同时下调",
            evidence_ids=["f1", "f2", "f3", "f4"],
            counterpoints=["也可能只是一次季度促销"],
            counterpoint_evidence_ids=["f4"],
            claim_scope=ClaimScope.SEGMENT,
        )
    ],
    expect=[
        (0, "bound_facts", "==", 4),
        (0, "bound_sources", "==", 4),
        (0, "bound_authors", "==", 4),
        # 全部四项都够，所以没有任何上限被触发
        (0, "evidence_support", "==", 1.0),
        (0, "source_diversity", "==", 1.0),
        (0, "counterpoint_awareness", ">", 0.0),
        (0, "specificity", ">=", 0.5),
        (0, "novelty", "==", 1.0),
        (0, "confidence", ">=", 0.75),
        (0, "stance", "is", ViewpointStance.STRONG),
        (0, "max_stance", "is", ViewpointStance.STRONG),
        (0, "scope_exceeds_evidence", "is", False),
        (0, "viewpoint_score", ">=", 70.0),
    ],
)


# ---------------------------------------------------------------------------
# 二、证据不足：允许 uncertain，不强行生成强观点
# ---------------------------------------------------------------------------

WEAK = Case(
    category="weak_evidence",
    note="一条事实、一条来源、四处未知；结论应当是 uncertain，而不是一个硬判断",
    pack=make_pack(
        [("f1", "某家厂商上调了企业版的起售档位", ["s1"])],
        unknowns=["是不是普遍现象", "幅度多大", "有没有第二家", "持续多久"],
    ),
    materials=make_materials(s1="erin"),
    drafts=[
        AngleDraft(claim="这家厂商也许在试探市场对涨价的接受度", evidence_ids=["f1"])
    ],
    expect=[
        (0, "bound_facts", "==", 1),
        (0, "bound_sources", "==", 1),
        (0, "evidence_support", "<=", 0.45),
        (0, "counterpoint_awareness", "==", 0.0),
        (0, "confidence", "<", 0.35),
        (0, "stance", "is", ViewpointStance.UNCERTAIN),
        (0, "max_stance", "is", ViewpointStance.UNCERTAIN),
    ],
)


# ---------------------------------------------------------------------------
# 三、证据冲突：同一批材料互相矛盾，判断要跟着软下来
# ---------------------------------------------------------------------------

CONFLICTING = Case(
    category="conflicting_evidence",
    note="材料之间互相矛盾；回应了反方的那条应当比装作没有争议的那条得分高",
    pack=make_pack(
        [
            ("f1", "一家第三方监测说企业版价格降了", ["s1"]),
            ("f2", "另一家监测说企业版价格其实没变", ["s2"]),
            ("f3", "两家监测的口径不一样", ["s3"]),
        ],
        consensus=["口径差异是分歧的来源"],
        counterpoints=["有一家的数据说价格其实没变", "降价的说法可能来自代理商的促销"],
        unknowns=["谁的口径更接近真实成交价"],
    ),
    materials=make_materials(s1="alice", s2="bob", s3="carol"),
    drafts=[
        AngleDraft(
            claim="降价的证据互相矛盾，只能说方向可能变了",
            evidence_ids=["f1", "f2", "f3"],
            counterpoints=["另一家的数据说价格没变"],
            counterpoint_evidence_ids=["f2"],
            claim_scope=ClaimScope.CASE,
            stance=ViewpointStance.CAUTIOUS,
        ),
        AngleDraft(
            claim="口径差异本身就说明定价在动",
            evidence_ids=["f1", "f2", "f3"],
            claim_scope=ClaimScope.CASE,
        ),
    ],
    expect=[
        # 第一条：模型**主动**选了比证据上界更保守的表达，这个选择被尊重
        (0, "stance_declared", "is", ViewpointStance.CAUTIOUS),
        (0, "stance", "is", ViewpointStance.CAUTIOUS),
        (0, "max_stance", "is", ViewpointStance.STRONG),
        (0, "bound_counterpoints", "==", 1),
        (0, "counterpoint_awareness", ">", 0.0),
        # 第二条：同一批证据、一个字都没提反方
        (1, "counterpoint_awareness", "==", 0.0),
        # 无反方上限恰好取到边界，所以写 `<=` 而不是 `<`：
        # 断言"上限生效了"的是下面那对 raw/最终值
        (1, "confidence", "<=", 0.65),
        (1, "confidence_raw", ">", 0.65),
        # 找过反方的那条整体更值得说（同一批证据、同一份打分公式）
        (0, "viewpoint_score", ">", 60.0),
    ],
)


# ---------------------------------------------------------------------------
# 四、单一来源：量的多少不能替质的不足说话
# ---------------------------------------------------------------------------

SINGLE_SOURCE = Case(
    category="single_source",
    note="三条事实全部来自同一条来源、同一位作者；支撑度与置信度都必须被压住",
    pack=make_pack(
        [
            ("f1", "这位作者说定价策略变了", ["s1"]),
            ("f2", "同一位作者说调整发生在上周", ["s1"]),
            ("f3", "同一位作者说企业版是重灾区", ["s1"]),
        ],
        counterpoints=["也有人说只是个别客户在谈价"],
        unknowns=["别的作者怎么看"],
    ),
    materials=make_materials(s1="frank"),
    drafts=[
        AngleDraft(
            claim="这家公司的定价策略正在改变",
            evidence_ids=["f1", "f2", "f3"],
            counterpoints=["也许只是个别客户在谈价"],
            counterpoint_evidence_ids=["f2"],
        )
    ],
    expect=[
        (0, "bound_facts", "==", 3),
        (0, "bound_sources", "==", 1),
        (0, "bound_authors", "==", 1),
        # 封顶前 raw 已经很高——所以"被压住"与"本来就低"在这里分得开
        (0, "evidence_support_raw", ">", 0.45),
        (0, "evidence_support", "<=", 0.45),
        (0, "source_diversity", "<", 0.5),
        (0, "confidence", "<=", 0.40),
        (0, "stance", "is", ViewpointStance.CAUTIOUS),
        (0, "max_stance", "is", ViewpointStance.CAUTIOUS),
    ],
)


# ---------------------------------------------------------------------------
# 五、有反方：说了不算，绑到事实才算
# ---------------------------------------------------------------------------

COUNTERPOINT = Case(
    category="counterpoint_present",
    note="证据包里已经有反方；绑到事实的那条比只写一句空话的那条得分高",
    pack=make_pack(
        [
            ("f1", "一家厂商把企业版价格下调了三成", ["s1"]),
            ("f2", "另一家把批量接口的单价调低了", ["s2"]),
            ("f3", "有采购方说收到了新的报价单", ["s3"]),
        ],
        counterpoints=["有分析认为这只是年度合同的常规重谈"],
        unknowns=["新报价的期限多长"],
    ),
    materials=make_materials(s1="alice", s2="bob", s3="carol"),
    drafts=[
        AngleDraft(
            claim="这一轮调价的方向是买方市场",
            evidence_ids=["f1", "f2", "f3"],
            counterpoints=["也可能只是年度合同的常规重谈"],
            counterpoint_evidence_ids=["f1"],
        ),
        AngleDraft(
            claim="买方议价的空间比去年大",
            evidence_ids=["f1", "f2", "f3"],
            counterpoints=["也许只是常规重谈"],
            counterpoint_evidence_ids=["f99"],
        ),
    ],
    expect=[
        (0, "bound_counterpoints", "==", 1),
        (0, "declared_counterpoints", "==", 1),
        # 第二条：话说了，但引不到任何真实事实
        (1, "declared_counterpoints", "==", 1),
        (1, "bound_counterpoints", "==", 0),
        # 绑定一条记满分的一半（1.0/2），声明两条不绑只有 0.35/2
        (0, "counterpoint_awareness", ">", 0.4),
        (1, "counterpoint_awareness", "<", 0.2),
    ],
)


# ---------------------------------------------------------------------------
# 六、overclaim：范围超出证据
# ---------------------------------------------------------------------------

OVERCLAIM = Case(
    category="overclaim",
    note="同一批证据（四条来源）被写成行业级判断；另一条只申报到细分，不该被标",
    pack=STRONG.pack,
    materials=STRONG.materials,
    drafts=[
        AngleDraft(
            claim="价格战的时代已经到来，整个行业都要重新定价",
            evidence_ids=["f1", "f2", "f3", "f4"],
            counterpoints=["也可能只是一次季度促销"],
            counterpoint_evidence_ids=["f4"],
            claim_scope=ClaimScope.SOCIETY,
        ),
        AngleDraft(
            claim="两家头部厂商的企业版定价同时松动",
            evidence_ids=["f1", "f2", "f3", "f4"],
            counterpoints=["也可能只是一次季度促销"],
            counterpoint_evidence_ids=["f4"],
            claim_scope=ClaimScope.SEGMENT,
        ),
    ],
    expect=[
        # 第一条：申报到 society，而证据只撑到 industry
        (0, "claim_scope", "is", ClaimScope.SOCIETY),
        (0, "evidence_scope", "is", ClaimScope.INDUSTRY),
        (0, "scope_exceeds_evidence", "is", True),
        (0, "breadth_markers", "contains", "时代"),
        (0, "breadth_markers", "contains", "整个"),
        (0, "max_stance", "is", ViewpointStance.MODERATE),
        # 第二条：同一批证据、同一个反方，只把范围申报对了
        (1, "claim_scope", "is", ClaimScope.SEGMENT),
        (1, "scope_exceeds_evidence", "is", False),
        (1, "max_stance", "is", ViewpointStance.STRONG),
    ],
)


# ---------------------------------------------------------------------------
# 七、预测：写成预测可以，写成必然不行
# ---------------------------------------------------------------------------

PREDICTION = Case(
    category="prediction",
    note="同一批证据，把预测写成必然的那条被判 overclaim，写成预测的那条没有",
    pack=make_pack(
        [
            ("f1", "两家厂商上周同时调整了企业版报价", ["s1"]),
            ("f2", "其中一家在给客户的邮件里提到了新的价目表", ["s1"]),
        ],
        counterpoints=["有客户说邮件里没有价格"],
        unknowns=["第三家会不会跟进"],
    ),
    materials=make_materials(s1="alice"),
    drafts=[
        AngleDraft(
            claim="第三家必然会在下个月跟进，这是注定的事",
            evidence_ids=["f1", "f2"],
        ),
        AngleDraft(
            claim="接下来或许还有第三家跟进",
            evidence_ids=["f1", "f2"],
        ),
    ],
    expect=[
        # 第一条：把关于将来的推断写成了必然
        (0, "breadth_markers", "contains", "必然"),
        (0, "breadth_markers", "contains", "注定"),
        (0, "scope_exceeds_evidence", "is", True),
        # 第二条：同一批证据、同一件事，写成预测就不触发
        (1, "breadth_markers", "empty", None),
        (1, "scope_exceeds_evidence", "is", False),
        # 两条都拿不到 strong：关于将来的判断，证据是现在的事实
        (0, "max_stance", "is", ViewpointStance.CAUTIOUS),
        (1, "max_stance", "is", ViewpointStance.CAUTIOUS),
    ],
)


#: 工作单第三十二节点名的七类，顺序即报告顺序。
CASES: tuple[Case, ...] = (
    STRONG,
    WEAK,
    CONFLICTING,
    SINGLE_SOURCE,
    COUNTERPOINT,
    OVERCLAIM,
    PREDICTION,
)

#: 七类的名字。`selftest.py` 会核对这里没有漏项或多项——工作单点了名的那几类
#: 必须真的在，否则"覆盖了七类"只是一句自述。
CATEGORIES: tuple[str, ...] = (
    "strong_evidence",
    "weak_evidence",
    "conflicting_evidence",
    "single_source",
    "counterpoint_present",
    "overclaim",
    "prediction",
)
