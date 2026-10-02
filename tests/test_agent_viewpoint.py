"""观点评估：确定性、"两个分数答两个问题"、以及"不许假装知道的事"。

工作单第十一、十二节要求 `confidence` 与 `viewpoint_score` **可复算、可解释、
可测试**，第十五节要求 novelty 与证据支撑**分开**，第九、十节要求"允许谨慎
判断"是一条**策略**而不是模型当天的心情。这个文件就是那几条的落点。

全部不碰数据库、不碰网络——打分是纯函数，它必须能被单独证明。
"""

from app.agent.schemas import (
    Angle,
    AngleDraft,
    ClaimScope,
    EvidenceFact,
    EvidencePack,
    ViewpointStance,
)
from app.agent.viewpoint import (
    CONFIDENCE_WEIGHTS,
    EVIDENCE_SCOPE_LADDER,
    LOW_SUPPORT_VIEWPOINT_CAP,
    MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE,
    SINGLE_SOURCE_CONFIDENCE_CAP,
    SINGLE_SOURCE_EVIDENCE_CAP,
    TIE_MARGIN,
    VIEWPOINT_WEIGHTS,
    authors_of,
    evidence_index,
    evidence_scope_for,
    find_breadth_markers,
    rank_key,
    score_counterpoint_awareness,
    score_source_diversity,
    score_unknown_pressure,
    score_viewpoints,
    select_viewpoint,
)


class Material:
    """一条研究材料的最小替身：只有打分用得到的两个字段。"""

    def __init__(self, author: str = "ada", content: str = "正文"):
        self.author_username = author
        self.content = content


def pack(*facts, consensus=(), counterpoints=(), unknowns=(), sources=()):
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


def make_index(p, materials=None):
    """用一份"材料 id → 材料"的表构造索引，id 与材料天然对齐。"""
    materials = dict(materials or {})
    ids = list(materials)
    return evidence_index(p, ids, [materials[i] for i in ids])


THREE_FACTS = pack(
    ("f1", "OpenAI 把 GPT-5 的 API 价格下调了四成", ["s1"]),
    ("f2", "Anthropic 同时下调了 Claude 的批量推理价格", ["s2"]),
    ("f3", "两家都在同一周内调整了企业版定价", ["s3"]),
    consensus=["厂商在争夺企业客户"],
    counterpoints=["降价可能只是季度促销"],
    unknowns=["是否长期"],
    sources=["s1", "s2", "s3"],
)
THREE_MATERIALS = {
    "s1": Material("alice", "OpenAI 降价"),
    "s2": Material("bob", "Anthropic 降价"),
    "s3": Material("carol", "两家同周调价"),
}


def draft(claim: str, **kw):
    return Angle(claim=claim, **kw)


# --------------------------------------------------------------------------
# 纯函数契约
# --------------------------------------------------------------------------


def test_same_input_yields_identical_result():
    """同一份输入两次调用，逐字段相同。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    drafts = [
        draft("企业级推理的定价权正在转向买方", evidence_ids=["f1", "f2", "f3"]),
        draft("这两家的降价节奏高度同步", evidence_ids=["f1", "f3"]),
    ]
    assert score_viewpoints(drafts, index) == score_viewpoints(drafts, index)


def test_input_order_does_not_change_per_viewpoint_numbers():
    """调换输入顺序，每个观点各自的数不变。

    `novelty` 要看同批其他观点，所以它天生依赖"批里有谁"——但**不该依赖顺序**。
    这条把它钉住。
    """
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    a = draft("企业级推理的定价权正在转向买方", evidence_ids=["f1", "f2", "f3"])
    b = draft("这两家的降价节奏高度同步", evidence_ids=["f1", "f3"])
    forward = {s.claim: s for s in score_viewpoints([a, b], index)}
    backward = {s.claim: s for s in score_viewpoints([b, a], index)}
    assert forward.keys() == backward.keys()
    for claim, score in forward.items():
        assert score == backward[claim]


def test_weights_sum_to_one():
    """权重和必须是 1，否则"0–100"这个刻度就是假的。"""
    assert abs(sum(w for _, w in VIEWPOINT_WEIGHTS) - 1.0) < 1e-9
    assert abs(sum(w for _, w in CONFIDENCE_WEIGHTS) - 1.0) < 1e-9


def test_confidence_does_not_contain_novelty():
    """工作单第十五节：novelty 必须与 evidence_support 分开。

    不是靠"读代码看起来没有"来保证，而是直接断言权重表里没有它——
    哪天有人把 novelty 加进置信度，这里会红。
    """
    assert {name for name, _ in CONFIDENCE_WEIGHTS} == {
        "evidence_support",
        "source_diversity",
        "counterpoint_awareness",
        "unknown_certainty",
    }


def test_changing_evidence_leaves_text_derived_scores_alone():
    """证据侧与文本侧的两组数互不串门。

    同一句 claim 换一批依据，具体性与全称量词判定的结果必须一模一样；
    而证据侧的数字必须真的跟着变。
    """
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    claim = "整个行业的定价权正在转向买方"
    one = score_viewpoints([draft(claim, evidence_ids=["f1"])], index)[0]
    three = score_viewpoints([draft(claim, evidence_ids=["f1", "f2", "f3"])], index)[0]

    assert one.specificity == three.specificity
    assert one.breadth_markers == three.breadth_markers
    assert (one.bound_facts, one.bound_sources) == (1, 1)
    assert (three.bound_facts, three.bound_sources) == (3, 3)
    assert one.evidence_support < three.evidence_support


# --------------------------------------------------------------------------
# 证据支撑与单一来源
# --------------------------------------------------------------------------


def test_single_source_caps_support_and_confidence():
    """只有一条来源时，支撑度与置信度都被压住，且**永远到不了 moderate**。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [draft("企业级推理的定价权正在转向买方", evidence_ids=["f1"])], index
    )[0]
    assert score.bound_sources == 1
    assert score.evidence_support <= SINGLE_SOURCE_EVIDENCE_CAP
    assert score.confidence <= SINGLE_SOURCE_CONFIDENCE_CAP
    assert score.stance in (ViewpointStance.UNCERTAIN, ViewpointStance.CAUTIOUS)
    # 封顶前的值必须留着，否则"被压过"与"本来就低"在报告里分不开。
    assert score.evidence_support_raw >= score.evidence_support


def test_low_support_caps_viewpoint_score():
    """支撑度不足时观点分被压到上限以下——"大胆但没证据"不能靠别的项救回来。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [draft("企业级推理的定价权正在转向买方", evidence_ids=["f1"])], index
    )[0]
    assert score.evidence_support < MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE
    assert score.viewpoint_score <= LOW_SUPPORT_VIEWPOINT_CAP


def test_well_supported_viewpoint_is_not_capped():
    """反证——上面那条如果对什么都压到 50，也会通过。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [
            draft(
                "企业级推理的定价权正在转向买方",
                evidence_ids=["f1", "f2", "f3"],
                counterpoints=["也许只是促销"],
                counterpoint_evidence_ids=["f1"],
            )
        ],
        index,
    )[0]
    assert score.evidence_support >= MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE
    assert score.viewpoint_score > LOW_SUPPORT_VIEWPOINT_CAP


def test_evidence_support_ignores_repeated_ids():
    """把 f1 写五遍不该看起来像五条事实。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    once = score_viewpoints([draft("判断一", evidence_ids=["f1"])], index)[0]
    five = score_viewpoints([draft("判断一", evidence_ids=["f1"] * 5)], index)[0]
    assert once.bound_facts == five.bound_facts == 1
    assert once.evidence_support == five.evidence_support


def test_unknown_ids_are_not_bound():
    """引不到真实事实的编号一个都不算。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints([draft("判断", evidence_ids=["f1", "f99"])], index)[0]
    assert score.bound_facts == 1


def test_missing_authors_do_not_merge_into_one():
    """拿不到作者名的来源不进作者集合，但条数要被报出来。

    补一个 "unknown" 占位会把三位作者静默算成一位——这正是要防的。
    """
    index = make_index(
        THREE_FACTS,
        {"s1": Material(""), "s2": Material(""), "s3": Material("bob")},
    )
    authors, missing = authors_of(["s1", "s2", "s3"], index)
    assert authors == ["bob"]
    assert missing == 2


def test_all_authors_missing_caps_diversity():
    """一条作者信息都没有时多样性被压住，而不是退化成只看来源数。"""
    index = make_index(
        THREE_FACTS,
        {"s1": Material(""), "s2": Material(""), "s3": Material("")},
    )
    score = score_viewpoints([draft("判断", evidence_ids=["f1", "f2", "f3"])], index)[0]
    assert score.bound_sources == 3
    assert score.bound_authors == 0
    assert score.source_diversity <= 0.6


def test_diversity_prefers_authors_over_sources():
    """三条来源但只有一位作者，不该拿到三条来源的满分。"""
    one_author = score_source_diversity(
        bound_sources=3, bound_authors=1, missing_author_sources=0
    )
    many_authors = score_source_diversity(
        bound_sources=3, bound_authors=4, missing_author_sources=0
    )
    assert one_author < many_authors


# --------------------------------------------------------------------------
# 反方
# --------------------------------------------------------------------------


def test_bound_counterpoints_outweigh_declared_ones():
    """绑到事实的反方比只写一句的值钱。

    阈值就是这么定的（`COUNTERPOINT_DECLARED_CREDIT = 0.35`）：写**两条**
    不绑的追不上绑一条；写满三条才会越过去——那已经是一次相当刻意的堆量，
    而"两条绑定即满分"的门槛仍在那之上。
    """
    bound_one = score_counterpoint_awareness(declared=1, bound=1)
    hollow_two = score_counterpoint_awareness(declared=2, bound=0)
    bound_two = score_counterpoint_awareness(declared=2, bound=2)
    assert bound_one > hollow_two
    assert bound_one < bound_two


def test_no_counterpoint_scores_zero():
    """一条反方都没有就是 0 分。"""
    assert score_counterpoint_awareness(declared=0, bound=0) == 0.0


def test_empty_counterpoint_strings_do_not_count():
    """空字符串不算"声明过"。统计非空的那一步在调用方，这里钉住结果。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    blank = draft("判断一", evidence_ids=["f1", "f2"], counterpoints=["", "  ", ""])
    score = score_viewpoints([blank], index)[0]
    assert score.declared_counterpoints == 0
    assert score.counterpoint_awareness == 0.0


def test_declared_but_unbound_counterpoint_is_credited_less():
    """写了反方但引不到事实时只拿一部分分，两个数字都留在报告里。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    unbound = score_viewpoints(
        [
            draft(
                "判断一",
                evidence_ids=["f1", "f2"],
                counterpoints=["也许只是清库存"],
                counterpoint_evidence_ids=["f99"],
            )
        ],
        index,
    )[0]
    bound = score_viewpoints(
        [
            draft(
                "判断一",
                evidence_ids=["f1", "f2"],
                counterpoints=["也许只是清库存"],
                counterpoint_evidence_ids=["f1"],
            )
        ],
        index,
    )[0]
    assert unbound.bound_counterpoints == 0
    assert bound.bound_counterpoints == 1
    assert unbound.counterpoint_awareness < bound.counterpoint_awareness


def test_ignoring_the_packs_counterpoints_is_stated_in_the_reasons():
    """证据包里有反方、观点一条都没回应时，理由里要写出来。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints([draft("判断一", evidence_ids=["f1", "f2"])], index)[0]
    assert score.counterpoint_awareness == 0
    assert any("一条都没有回应" in reason for reason in score.reasons)


# --------------------------------------------------------------------------
# 未知压力：不可被模型自报操纵
# --------------------------------------------------------------------------


def test_declaring_fewer_unknowns_cannot_lower_pressure():
    """模型少写未知项不能把压力压下去——压力来自未被引用的材料。"""
    quiet = pack(("f1", "事实一", ["s1"]), unknowns=[], sources=["s1"])
    index = evidence_index(
        quiet, ["s1", "s2", "s3"], [Material(), Material(), Material()]
    )
    value, unreferenced, total = score_unknown_pressure(index)
    assert (total, unreferenced) == (3, 2)
    assert value >= 2 / 3

    loud = pack(("f1", "事实一", ["s1"]), unknowns=["a", "b", "c", "d"], sources=["s1"])
    value_loud, _, _ = score_unknown_pressure(
        evidence_index(loud, ["s1", "s2", "s3"], [Material(), Material(), Material()])
    )
    # 多写未知只会把压力顶上去，绝不会把外生的那一项按下来。
    assert value_loud >= value


def test_declaring_more_unknowns_can_raise_pressure():
    """反过来也要成立，否则"自报未知"这一路输入就没在起作用。"""
    base = evidence_index(
        pack(("f1", "事实一", ["s1"]), sources=["s1"]), ["s1"], [Material()]
    )
    noisy = evidence_index(
        pack(("f1", "事实一", ["s1"]), unknowns=["a", "b", "c", "d"], sources=["s1"]),
        ["s1"],
        [Material()],
    )
    assert score_unknown_pressure(noisy)[0] > score_unknown_pressure(base)[0]


def test_duplicate_material_ids_do_not_inflate_the_denominator():
    """重复的材料 id 会把分母灌大，从而**免费**压低未知压力。"""
    p = pack(("f1", "事实一", ["s1"]), sources=["s1"])
    index = evidence_index(p, ["s1", "s1", "s1"], [Material(), Material(), Material()])
    assert score_unknown_pressure(index) == (0.0, 0, 1)


def test_link_only_materials_are_excluded_from_the_denominator():
    """纯链接材料产出不了事实，不该算成"这条材料什么都没产出"。"""
    p = pack(("f1", "事实一", ["s1"]), sources=["s1"])
    index = evidence_index(
        p,
        ["s1", "s2"],
        [Material("ada", "有正文"), Material("bob", "https://t.co/abc")],
    )
    _, unreferenced, total = score_unknown_pressure(index)
    assert (total, unreferenced) == (1, 0)


# --------------------------------------------------------------------------
# 新颖性
# --------------------------------------------------------------------------


def test_restating_a_fact_is_not_novel():
    """把事实换个标点重说一遍不是判断。n-gram 重合要抓得住。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [draft("OpenAI 把 GPT-5 的 API 价格下调了四成", evidence_ids=["f1"])], index
    )[0]
    assert score.restates_fact is True
    assert score.novelty == 0.0


def test_duplicate_siblings_are_both_penalised():
    """同批里两条几乎一样的观点，两个都记 0（不保一个杀一个）。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    shared = "企业级推理的定价权正在从厂商转向买方"
    scores = score_viewpoints(
        [
            draft(shared, evidence_ids=["f1", "f2"]),
            draft(shared + "，这一点已经明确", evidence_ids=["f1", "f2"]),
        ],
        index,
    )
    assert all(s.novelty == 0.0 for s in scores)
    assert scores[0].duplicate_group == scores[1].duplicate_group != ""


def test_two_unrelated_claims_are_not_duplicates():
    """反证——上面那条如果对什么都判重复，也会通过。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    scores = score_viewpoints(
        [
            draft("企业级推理的定价权正在从厂商转向买方", evidence_ids=["f1"]),
            draft("两家选择的降价时点值得单独观察", evidence_ids=["f1"]),
        ],
        index,
    )
    assert all(s.novelty == 1.0 for s in scores)
    assert all(s.duplicate_group == "" for s in scores)


def test_declared_restatement_lowers_novelty():
    """模型自述"只是复述"时 novelty 归零——它只能调低，不能调高。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    claim = "企业级推理的定价权正在从厂商转向买方"
    plain = score_viewpoints([draft(claim, evidence_ids=["f1"])], index)[0]
    honest = score_viewpoints(
        [draft(claim, evidence_ids=["f1"], restates_known_material=True)], index
    )[0]
    assert plain.novelty == 1.0
    assert honest.novelty == 0.0


def test_declaring_restatement_false_cannot_raise_novelty():
    """自述"不是复述"拿不到任何加分——那是一个只能向下的开关。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    plain = score_viewpoints([draft("判断一", evidence_ids=["f1", "f2"])], index)[0]
    loud = score_viewpoints(
        [
            draft(
                "判断一",
                evidence_ids=["f1", "f2"],
                restates_known_material=False,
                novelty_note="全网首发",
            )
        ],
        index,
    )[0]
    assert loud.novelty == plain.novelty


def test_originality_claim_is_flagged_but_not_rewarded():
    """工作单第三十一节：不能核验的"全网原创"不构成新颖性。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [
            draft(
                "这是一个全网首创的判断，颠覆了已有认知",
                evidence_ids=["f1", "f2"],
            )
        ],
        index,
    )[0]
    assert score.originality_claim is True
    assert any("无法核验" in reason for reason in score.reasons)


def test_novelty_is_gated_by_evidence_support():
    """没有证据的大胆判断不能因为"新"拿到高分。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    claim = "一个全新的因果解释：定价权的转移其实是组织结构的转移"
    thin = score_viewpoints([draft(claim, evidence_ids=["f1"])], index)[0]
    solid = score_viewpoints([draft(claim, evidence_ids=["f1", "f2", "f3"])], index)[0]
    assert thin.newness_contribution < thin.novelty
    assert solid.newness_contribution == solid.novelty
    assert solid.viewpoint_score > thin.viewpoint_score


def test_short_claims_do_not_trigger_span_detection():
    """比阈值短的观点不会被误判成复述。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints([draft("降价了", evidence_ids=["f1"])], index)[0]
    assert score.restates_fact is False


# --------------------------------------------------------------------------
# 范围与 overclaim
# --------------------------------------------------------------------------


def test_evidence_scope_ladder():
    """一档一档往上走，每一档都要能到。"""
    assert (
        evidence_scope_for(bound_facts=1, bound_sources=1, bound_authors=1)
        is ClaimScope.FACT
    )
    assert (
        evidence_scope_for(bound_facts=2, bound_sources=1, bound_authors=1)
        is ClaimScope.CASE
    )
    assert (
        evidence_scope_for(bound_facts=2, bound_sources=2, bound_authors=2)
        is ClaimScope.SEGMENT
    )
    assert (
        evidence_scope_for(bound_facts=4, bound_sources=3, bound_authors=3)
        is ClaimScope.INDUSTRY
    )


def test_society_scope_is_unreachable_in_one_run():
    """工作单第二十八、二十九节："这是一个时代的转折"应当结构上挣不到。"""
    _, min_sources, min_authors = next(
        entry for entry in EVIDENCE_SCOPE_LADDER if entry[0] is ClaimScope.SOCIETY
    )
    assert min_sources > 3 and min_authors > 3


def test_declared_scope_wider_than_evidence_is_flagged():
    """两条产品降价 ≠ 整个行业进入价格战。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [
            draft(
                "整个行业已经进入价格战",
                evidence_ids=["f1"],
                claim_scope=ClaimScope.SOCIETY,
            )
        ],
        index,
    )[0]
    assert score.evidence_scope is ClaimScope.FACT
    assert score.scope_exceeds_evidence is True
    assert any("超过证据能支撑的" in reason for reason in score.reasons)


def test_narrow_declaration_cannot_hide_broad_prose():
    """申报了最窄的范围，正文却写成全称判断——照样标出来。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [
            draft(
                "所有厂商都必然跟进降价",
                evidence_ids=["f1"],
                claim_scope=ClaimScope.FACT,
            )
        ],
        index,
    )[0]
    assert score.claim_scope is ClaimScope.FACT
    assert score.scope_exceeds_evidence is True
    assert score.breadth_markers == ("所有", "必然")


def test_within_scope_is_not_flagged():
    """反证——上面两条如果对什么都判超范围，也会通过。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [
            draft(
                "定价权正在转向买方",
                evidence_ids=["f1", "f2", "f3"],
                claim_scope=ClaimScope.SEGMENT,
            )
        ],
        index,
    )[0]
    assert score.scope_exceeds_evidence is False


def test_overclaim_caps_stance():
    """范围超出证据时立场上界被压低——超证据的承诺不能交付。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    common = dict(
        evidence_ids=["f1", "f2", "f3"],
        counterpoints=["也许只是促销"],
        counterpoint_evidence_ids=["f1"],
    )
    narrow = score_viewpoints(
        [draft("定价权正在转向买方", claim_scope=ClaimScope.SEGMENT, **common)], index
    )[0]
    broad = score_viewpoints(
        [draft("定价权正在转向买方", claim_scope=ClaimScope.SOCIETY, **common)], index
    )[0]
    assert narrow.scope_exceeds_evidence is False
    assert broad.scope_exceeds_evidence is True
    assert broad.max_stance is not ViewpointStance.STRONG
    assert any("上界下调为" in reason for reason in broad.reasons)


def test_hedge_words_are_not_breadth_markers():
    """`一定` 落在 `一定程度上` 里，那是一个诚实的措辞，不是全称判断。"""
    assert find_breadth_markers("这在一定程度上说明问题") == []
    assert find_breadth_markers("这一定会发生") == ["一定"]


def test_missing_scope_is_recorded_not_invented():
    """没申报就记没申报，不给一个方便的默认值。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints([draft("判断一", evidence_ids=["f1", "f2"])], index)[0]
    assert score.claim_scope is None
    assert score.scope_declared_missing is True
    assert any("没有申报 claim_scope" in reason for reason in score.reasons)


# --------------------------------------------------------------------------
# 立场上界
# --------------------------------------------------------------------------


def test_a_draft_stance_is_read_as_the_declaration():
    """喂 `AngleDraft` 时，`stance` **就是**模型的申报（草稿上没有别的东西）。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [
            AngleDraft(
                claim="定价权正在转向买方",
                evidence_ids=["f1", "f2", "f3"],
                counterpoints=["也许只是促销"],
                counterpoint_evidence_ids=["f1"],
                stance=ViewpointStance.CAUTIOUS,
            )
        ],
        index,
    )[0]
    assert score.stance_declared is ViewpointStance.CAUTIOUS
    assert score.stance is ViewpointStance.CAUTIOUS


def test_stance_cannot_exceed_the_evidence_ceiling():
    """模型申报 strong 但证据只支持到 uncertain 时，被压下来并写明理由。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [
            # 喂 `Angle` 时申报值读的是 `stance_declared`，不是 `stance`：
            # 后者的含义是"钳过之后**生效**的立场"，拿它当申报会让打分函数
            # 对自己的输出不幂等（见 `viewpoint.declared_stance_of`）。
            draft(
                "定价权正在转向买方",
                evidence_ids=["f1"],
                stance_declared=ViewpointStance.STRONG,
            )
        ],
        index,
    )[0]
    assert score.stance_declared is ViewpointStance.STRONG
    assert score.max_stance is ViewpointStance.UNCERTAIN
    assert score.stance is ViewpointStance.UNCERTAIN
    assert any("已下调" in reason for reason in score.reasons)


def test_more_conservative_than_the_ceiling_is_allowed():
    """证据支持 strong 时模型仍可选择更保守——工作单第九节。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [
            draft(
                "定价权正在转向买方",
                evidence_ids=["f1", "f2", "f3"],
                counterpoints=["也许只是促销"],
                counterpoint_evidence_ids=["f1"],
                stance_declared=ViewpointStance.CAUTIOUS,
            )
        ],
        index,
    )[0]
    assert score.max_stance is ViewpointStance.STRONG
    assert score.stance is ViewpointStance.CAUTIOUS


def test_scoring_is_idempotent_on_its_own_output():
    """拿打出来的 `Angle` 再打一遍，逐字段与第一遍相同。

    这条钉的是一件真实发生过的事：`select_angle` 会用**同一批 `Angle`**
    复算一遍分数（`tools/angles.py`），而第一遍写在 `Angle.stance` 上的
    是钳过之后的**生效值**。如果打分函数把它读成"模型申报了这个值"，
    第二遍的 `stance_declared` 就会凭空多出一个，`confidence` 与
    `viewpoint_score` 也可能跟着变——state 里于是同时存在"存下来的分"与
    "排序依据的分"，而两者不一致时没有任何地方会报错。
    """
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    angles = [
        Angle(
            claim="定价权正在转向买方",
            evidence_ids=["f1", "f2", "f3"],
            counterpoints=["也许只是促销"],
            counterpoint_evidence_ids=["f1"],
            claim_scope=ClaimScope.SEGMENT,
        ),
        Angle(claim="这两家的降价节奏高度同步", evidence_ids=["f1", "f3"]),
    ]
    first = score_viewpoints(angles, index)
    # 把第一遍的结果写回 `Angle`（这正是生产链路做的），再打一遍。
    stored = [
        angle.model_copy(
            update={
                "confidence": score.confidence,
                "viewpoint_score": score.viewpoint_score,
                "stance": score.stance,
                "stance_declared": score.stance_declared,
                "max_stance": score.max_stance,
                "claim_scope": score.claim_scope,
                "evidence_scope": score.evidence_scope,
            }
        )
        for angle, score in zip(angles, first)
    ]
    second = score_viewpoints(stored, index)
    assert first == second
    # 没申报的那条，两遍都必须记"没申报"，而不是把生效值当成申报值收下。
    assert first[1].stance_declared is None
    assert second[1].stance_declared is None


def test_undeclared_stance_takes_the_ceiling():
    """没申报时取上界，并把这件事写进理由。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [draft("定价权正在转向买方", evidence_ids=["f1", "f2", "f3"])], index
    )[0]
    assert score.stance is score.max_stance
    assert any("未声明立场" in reason for reason in score.reasons)


def test_no_counterpoint_caps_confidence():
    """没有对手的强判断不该出现。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints(
        [draft("定价权正在转向买方", evidence_ids=["f1", "f2", "f3"])], index
    )[0]
    assert score.counterpoint_awareness == 0
    assert score.confidence <= 0.65
    assert score.confidence < score.confidence_raw


def test_weak_evidence_yields_an_honest_uncertain():
    """证据不足时表态为 uncertain——这是一个合法结果，不是失败（工作单第九节）。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    score = score_viewpoints([draft("也许有变化", evidence_ids=["f1"])], index)[0]
    assert score.confidence < 0.35
    assert score.stance is ViewpointStance.UNCERTAIN


# --------------------------------------------------------------------------
# 排序与选择
# --------------------------------------------------------------------------


def test_rank_key_is_total_and_deterministic():
    """分数、置信度、来源数全相同时，仍有一个确定的先后。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    scores = score_viewpoints(
        [draft("判断乙", evidence_ids=["f1"]), draft("判断甲", evidence_ids=["f1"])],
        index,
    )
    assert [s.claim for s in sorted(scores, key=rank_key)] == sorted(
        ["判断甲", "判断乙"]
    )


def test_high_score_wins_regardless_of_input_order():
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    strong = draft(
        "强判断：定价权正在转向买方",
        evidence_ids=["f1", "f2", "f3"],
        counterpoints=["也许只是促销"],
        counterpoint_evidence_ids=["f1"],
    )
    weak = draft("弱判断", evidence_ids=["f1"])
    for pair in ([strong, weak], [weak, strong]):
        picked = select_viewpoint(score_viewpoints(pair, index))
        assert picked is not None
        assert picked.claim == "强判断：定价权正在转向买方"
        assert picked.ambiguous is False


def test_close_scores_are_flagged_ambiguous():
    """分差很小的时候判为并列，但结果不会悬空——流程要往下走。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    scores = score_viewpoints(
        [
            draft("判断甲", evidence_ids=["f1", "f2"]),
            draft("判断乙", evidence_ids=["f1", "f2"]),
        ],
        index,
    )
    picked = select_viewpoint(scores)
    assert picked is not None
    assert picked.ambiguous is True
    assert picked.margin is not None and picked.margin <= TIE_MARGIN
    assert "并列区间" in picked.how


def test_target_inside_the_tie_band_is_honoured():
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    scores = score_viewpoints(
        [draft("判断甲", evidence_ids=["f1"]), draft("判断乙", evidence_ids=["f1"])],
        index,
    )
    picked = select_viewpoint(scores, target="判断乙")
    assert picked is not None
    assert picked.claim == "判断乙"


def test_target_outside_the_tie_band_is_ignored_with_a_number():
    """工作单第二十节：排序明确时不让模型推翻；回退要说明差了多少分。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    scores = score_viewpoints(
        [
            draft(
                "强判断：定价权正在转向买方",
                evidence_ids=["f1", "f2", "f3"],
                counterpoints=["也许只是促销"],
                counterpoint_evidence_ids=["f1"],
            ),
            draft("弱判断", evidence_ids=["f1"]),
        ],
        index,
    )
    picked = select_viewpoint(scores, target="弱判断")
    assert picked is not None
    assert picked.claim == "强判断：定价权正在转向买方"
    # 回退必须带上"差了多少分"，而不是只说一句"按排序选定"。
    assert f"超过容差 {TIE_MARGIN:g}" in picked.how


def test_unknown_target_falls_back_to_the_ranking():
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    scores = score_viewpoints([draft("判断一", evidence_ids=["f1"])], index)
    picked = select_viewpoint(scores, target="不存在的观点")
    assert picked is not None
    assert picked.claim == "判断一"
    assert "不是有效观点" in picked.how


def test_single_candidate_is_never_ambiguous():
    """只有一个候选时没有"前二名"，不能凭空报一个 0 分差。"""
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    picked = select_viewpoint(
        score_viewpoints([draft("判断一", evidence_ids=["f1"])], index)
    )
    assert picked is not None
    assert picked.ambiguous is False
    assert picked.margin is None


def test_empty_selection_returns_none():
    assert select_viewpoint([]) is None


# --------------------------------------------------------------------------
# 反证：打分器确实在动
# --------------------------------------------------------------------------


def test_the_scorer_actually_moves_with_its_inputs():
    """反证——上面那些"相等"的断言，如果打分器根本不看输入，也全会通过。

    这里把三类输入各改一次，要求对应的那一项真的变了。没有这条，一个
    "永远返回同一组数"的实现能让本文件大部分测试变绿。
    """
    index = make_index(THREE_FACTS, THREE_MATERIALS)
    claim = "定价权正在转向买方"
    base = score_viewpoints([draft(claim, evidence_ids=["f1"])], index)[0]
    assert base.specificity == 0.0

    more_facts = score_viewpoints(
        [draft(claim, evidence_ids=["f1", "f2", "f3"])], index
    )[0]
    assert more_facts.evidence_support != base.evidence_support
    assert more_facts.source_diversity != base.source_diversity

    with_counterpoint = score_viewpoints(
        [
            draft(
                claim,
                evidence_ids=["f1"],
                counterpoints=["也许只是促销"],
                counterpoint_evidence_ids=["f1"],
            )
        ],
        index,
    )[0]
    assert with_counterpoint.counterpoint_awareness != base.counterpoint_awareness

    specific = score_viewpoints(
        [draft("GPT-5 与 Claude 在 2026 年同周降价四成", evidence_ids=["f1"])], index
    )[0]
    assert specific.specificity > base.specificity
