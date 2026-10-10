"""话题背景（Context）的边界守卫与陌生读者信号（工作单第三～十五节）。

这个文件钉住两类**确定性**行为，它们都对应一个真实的失败：

1. `sanitize_context`：模型给的背景里，无据可依的东西必须被删掉，而不是"更委婉地说".
   真机故障的背面是模型很会写"市场规模多少亿"，而这类话一旦进了 Writer 的 prompt，
   就会被写成一条看起来有据可依的句子。
2. `context_signals`：真机那份稿子（首句「2.2 万星的开源库……」）必须被判出
   "概念跳步"与"证据劫持"，否则 Critic 拿不到任何提示，仍会给它一个高 Context Fit。

**这里全是纯函数，不调 LLM、不碰数据库。**
"""

from app.agent.context import (
    ContextSignals,
    context_signals,
    context_text,
    find_unsupported_claims,
    first_sentence,
    is_empty,
    sanitize_context,
    sentences,
)
from app.agent.prompts import format_context, format_context_signals
from app.agent.schemas import (
    Candidate,
    ContextFit,
    ContextOrigin,
    EvidenceFact,
    EvidencePack,
    TopicContext,
)

# ---------------------------------------------------------------------------
# 真机样本：run 里那条被拦住的草稿（工作单第十四节要求用它当 fixture）
#
# 选题是"华尔街热议的新型云"。作者（Agent）在研究里已经知道新型云指什么、
# 那个开源库与它什么关系，于是**把一条事实当成了话题的入口**——读者第一句
# 被一个陌生的具体项目拦住，看不出这整篇在讲哪个话题。
# ---------------------------------------------------------------------------
REAL_TOPIC = "华尔街热议的「新型云」到底是什么"
REAL_DRAFT = (
    "2.2 万星的开源库，把行情、财报、资金流几千个接口免费开放。"
    "过去要自建系统才能做的事，正在下放给个人。"
    "新型云里的去中心化 GPU 云就落在这一层：收入来自算力单价，而不是帮客户跨过门槛。"
    "币安的意图只是「可能」，但只卖 GPU 的云，估值逻辑已经站不稳。"
)
REAL_FACT = "这个开源库把行情、财报、资金流几千个接口免费开放"

REAL_CONTEXT = TopicContext(
    subject="新型云，指的是最近被热议的一批云服务模式",
    definition="去中心化 GPU 云是其中一类：卖的是算力单价，而不是帮客户跨过门槛",
    why_now="算力价格与模型成本同时下降，这类云开始被讨论",
    core_tension="只卖算力的云，估值逻辑与帮客户省事的云不一样",
    origin=ContextOrigin.FACT,
    source_ids=["s1"],
)

KNOWN = {"s1", "s2"}


def real_pack() -> EvidencePack:
    return EvidencePack(
        facts=[EvidenceFact(fact_id="f1", fact=REAL_FACT, source_ids=["s1"])],
        context=REAL_CONTEXT,
    )


def candidate(content: str, candidate_id: str = "c1") -> Candidate:
    return Candidate(candidate_id=candidate_id, label="直接观点", content=content)


# ---------------------------------------------------------------------------
# 空与不空
# ---------------------------------------------------------------------------


def test_context_is_empty_only_when_all_four_fields_are_blank():
    """只填 `subject` 的一份背景仍然有用——它就是"这篇在讲什么"。"""
    assert is_empty(None) is True
    assert is_empty(TopicContext()) is True
    assert is_empty(TopicContext(subject="   ")) is True
    assert is_empty(TopicContext(subject="新型云指什么")) is False
    # origin / source_ids 是元数据，不参与这个判断
    assert is_empty(TopicContext(origin=ContextOrigin.FACT, source_ids=["s1"])) is True


def test_context_text_order_is_fixed():
    """顺序固定，所有 n-gram 判定才可复算。"""
    assert context_text(REAL_CONTEXT).splitlines()[0].startswith("新型云")
    assert context_text(None) == ""


# ---------------------------------------------------------------------------
# 边界守卫：外部事实 / 用户前提 / Agent 推断（工作单第五节）
# ---------------------------------------------------------------------------


def test_fact_origin_with_unverifiable_sources_is_downgraded_to_inference():
    """声称来自材料，就得给出真实出处；给不出就降级——不许它冒充事实。

    降级之后它**失去豁免**，所以那句"某公司拥有 5 万台 GPU"随之下线：
    量级数字在无出处的背景里一律算编造。
    """
    context = TopicContext(
        subject="这类云被讨论得很多。某公司拥有 5 万台 GPU。",
        origin=ContextOrigin.FACT,
        source_ids=["编的"],
    )

    cleaned, notes = sanitize_context(context, KNOWN)

    assert cleaned is not None
    assert cleaned.origin is ContextOrigin.INFERENCE
    assert cleaned.source_ids == []
    assert cleaned.subject == "这类云被讨论得很多。"
    assert any("降级" in note for note in notes)


def test_non_fact_origin_never_keeps_source_ids():
    """用户前提与推断没有"出处"这回事——留着它只会让下游以为核验过。"""
    context = TopicContext(
        subject="今天关注美股",
        origin=ContextOrigin.PREMISE,
        source_ids=["s1"],
    )

    cleaned, notes = sanitize_context(context, KNOWN)

    assert cleaned.origin is ContextOrigin.PREMISE
    assert cleaned.source_ids == []
    assert any("source_ids" in note for note in notes)


def test_unsourced_magnitude_claims_are_dropped_sentence_by_sentence():
    """整句丢掉，**不是**整段丢、也不是改写成模糊说法。

    只丢命中那一句：同一段里没问题的句子要留下——过度删除会把背景清空，
    而那会让 Writer 收到"没有背景"，等于把这个故障换了个位置。
    """
    context = TopicContext(
        subject="这个市场最近很热。市场规模很快就会到 3000 亿。云厂商都在往里挤。",
        origin=ContextOrigin.INFERENCE,
    )

    cleaned, notes = sanitize_context(context, KNOWN)

    assert "3000 亿" not in cleaned.subject
    assert "这个市场最近很热。" in cleaned.subject
    assert "云厂商都在往里挤。" in cleaned.subject
    assert notes


def test_banned_phrases_are_dropped_even_when_the_context_is_backed_by_sources():
    """「业内普遍认为」这类**措辞**本身就是结论，有出处也不许留在背景里。

    背景只负责让读者进门，不负责下判断——这是工作单第四、七节的硬边界，
    与"有没有引用"是两件事，所以它不受 `origin=fact` 豁免。
    """
    context = TopicContext(
        subject="业内普遍认为这类云会取代传统云。算力单价在下降。",
        origin=ContextOrigin.FACT,
        source_ids=["s1"],
    )

    cleaned, _ = sanitize_context(context, KNOWN)

    assert "业内普遍认为" not in cleaned.subject
    assert "算力单价在下降。" in cleaned.subject


def test_backed_fact_context_may_carry_numbers():
    """材料里写了的数字就不是编的——有出处的事实性背景豁免量级检查。"""
    context = TopicContext(
        subject="这个开源库拿到了 2.2 万星。",
        origin=ContextOrigin.FACT,
        source_ids=["s1"],
    )

    cleaned, notes = sanitize_context(context, KNOWN)

    assert cleaned is not None
    assert cleaned.subject == "这个开源库拿到了 2.2 万星。"
    assert notes == []


def test_context_that_becomes_empty_after_trimming_returns_none():
    """清完什么都不剩就返回 None：本次没有背景，下游按"没有背景"渲染。

    空着比编一段更诚实（工作单第六节）——返回一个空对象会让 prompt 里出现
    一个看起来有、其实没内容的块。
    """
    context = TopicContext(subject="市场规模会到 3000 亿。")

    cleaned, notes = sanitize_context(context, KNOWN)

    assert cleaned is None
    assert notes


def test_missing_context_is_not_an_error():
    """模型漏了背景不是错误路径：返回 None，不抛异常、不编一段补上。"""
    assert sanitize_context(None, KNOWN) == (None, [])


def test_find_unsupported_claims_returns_the_hits_verbatim():
    """命中项逐字回传，便于核对误报；没命中就是空列表，不是空话。"""
    hits = find_unsupported_claims("华尔街一致认为，市场规模 300 亿。")
    assert "华尔街一致认为" in hits
    assert any("300 亿" in hit for hit in hits)
    assert find_unsupported_claims("算力单价在下降。") == []


def test_sentences_and_first_sentence():
    assert sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]
    assert first_sentence(REAL_DRAFT).startswith("2.2 万星的开源库")
    assert first_sentence("") == ""


# ---------------------------------------------------------------------------
# 陌生读者信号：真机那份稿子必须被认出来（工作单第十二、十四节）
# ---------------------------------------------------------------------------


def test_the_real_failure_is_flagged_as_a_concept_jump_and_evidence_hijacking():
    """真机故障：首句复述了一条事实，而话题本身还没交代。

    `context_missing` 刻意是 **False**：这条稿子确实提到了"去中心化 GPU 云"
    （它没有整篇不接背景），坏的地方在**入口**——第一句把一条事实当成了话题的入口。
    把两个信号混成一个会让"没接背景"与"入口是事实"这两件事分不开，
    而它们的改法完全不同。
    """
    pack = real_pack()

    signals = context_signals(pack.context, [candidate(REAL_DRAFT)], pack)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.evidence_hijacking is True
    assert signal.hijacked_fact_id == "f1"
    assert signal.concept_jump is True
    assert "2.2 万" in signal.entry_specifics
    assert signal.context_missing is False


def test_a_draft_that_shares_nothing_with_the_background_is_context_missing():
    """整篇与背景没有任何共同表述 ⇒ 这篇没有接住背景。"""
    pack = real_pack()

    signal = context_signals(
        pack.context, [candidate("今天天气不错，适合出门走走。")], pack
    )[0]

    assert signal.context_missing is True


def test_evidence_hijacking_needs_an_actual_restatement_of_a_fact():
    """只共享一个数字不算"把事实当入口"——判的是复述，不是撞词。

    这条防的是**误报**：首句提到同一个量（比如都写了"四成"）是共享一个数，
    不是搬运一条事实。误报会让 Critic 学会忽略这组信号。
    """
    pack = EvidencePack(
        facts=[EvidenceFact(fact_id="f1", fact=REAL_FACT, source_ids=["s1"])],
        context=REAL_CONTEXT,
    )

    signal = context_signals(
        pack.context, [candidate("新型云里的公司今年融资涨了四成。")], pack
    )[0]

    assert signal.evidence_hijacking is False
    assert signal.hijacked_fact_id == ""


def test_a_draft_that_opens_on_the_topic_is_clean():
    """先交代话题、再给事实——同一批材料，正确写法不该被标记。"""
    pack = real_pack()
    good = (
        "新型云正在被热议：有一类云只卖算力，不帮客户跨过门槛。"
        "这个开源库把行情、财报、资金流几千个接口免费开放，就是这一层的典型。"
    )

    signal = context_signals(pack.context, [candidate(good)], pack)[0]

    assert signal.evidence_hijacking is False
    assert signal.concept_jump is False
    assert signal.context_missing is False


def test_signals_are_silent_without_a_context():
    """没有背景可承接，就谈不上"没接住"——那种运行里报出来只会是噪声。"""
    pack = EvidencePack(facts=[EvidenceFact(fact_id="f1", fact=REAL_FACT, source_ids=["s1"])])

    assert context_signals(pack.context, [candidate(REAL_DRAFT)], pack) == []
    assert context_signals(None, [candidate(REAL_DRAFT)], None) == []


def test_signals_are_pure_and_repeatable():
    """纯函数：同一份输入连算两次，结果逐位相同（同 app/style/signals.py 的纪律）。"""
    pack = real_pack()
    first = context_signals(pack.context, [candidate(REAL_DRAFT)], pack)
    second = context_signals(pack.context, [candidate(REAL_DRAFT)], pack)
    assert first == second
    assert isinstance(first[0], ContextSignals)


# ---------------------------------------------------------------------------
# 渲染：永远返回文本，绝不返回空串（`render()` 不会因为缺键报错）
# ---------------------------------------------------------------------------


def test_format_context_never_returns_an_empty_string():
    """漏传 `{{CONTEXT}}` 的代价是字面量进 prompt，所以无背景时也要有话说。"""
    for value in (None, TopicContext()):
        text = format_context(value)
        assert text.strip()
        assert "{{" not in text
        # 无背景时的说明必须堵住"自己补一段"这条路
        assert "不要为了补背景去编" in text


def test_format_context_carries_the_four_fields_and_the_origin():
    text = format_context(REAL_CONTEXT)

    assert "新型云，指的是最近被热议的一批云服务模式" in text
    assert "去中心化 GPU 云是其中一类" in text
    assert "算力价格与模型成本同时下降" in text
    assert "只卖算力的云" in text
    # 来源必须写明：它是"这段能不能拿来当依据"的唯一线索
    assert "外部事实" in text


def test_format_context_switches_the_purpose_line_for_the_angle_stage():
    """同一份内容、两套用途说明：给 Angle 的那份要堵住"从背景推结论"。"""
    reader = format_context(REAL_CONTEXT, for_reader=True)
    angle = format_context(REAL_CONTEXT, for_reader=False)

    assert "读者不知道这个话题" in reader
    assert "读者不知道这个话题" not in angle
    assert "不是判断" in angle
    # 两边的背景内容必须逐字相同，否则两边看到的不是同一段背景
    assert "去中心化 GPU 云是其中一类" in reader
    assert "去中心化 GPU 云是其中一类" in angle


def test_format_context_signals_hands_the_critic_the_concrete_hit():
    """Critic 必须看到**具体命中了什么**，不能只看到一个"有问题"。"""
    pack = real_pack()

    text = format_context_signals(pack.context, [candidate(REAL_DRAFT)], pack)

    assert "[c1]" in text
    assert "2.2 万" in text  # 回传具体对象，便于核对误报
    assert "f1" in text  # 指出复述的是哪条事实
    assert "不是分数" in text  # 明确它只作参考事实


def test_format_context_signals_says_so_when_there_is_no_context():
    """没有背景时印一句"本次不适用"，而不是印一排"未发现"。"""
    pack = EvidencePack(facts=[])

    text = format_context_signals(None, [candidate(REAL_DRAFT)], pack)

    assert "没有话题背景可参照" in text


# ---------------------------------------------------------------------------
# 结构：ContextFit 与 CriticResult 的接线
# ---------------------------------------------------------------------------


def test_context_fit_is_a_separate_group_from_viewpoint_fit():
    """与 viewpoint_fit 并排、**不给总分**（同 ViewpointFit 的理由）。"""
    fit = ContextFit(
        reader_can_follow=False, context_missing=6.0, concept_jump=4.0, evidence_hijacking=2.0
    )

    assert set(ContextFit.model_fields) == {
        "reader_can_follow",
        "context_missing",
        "concept_jump",
        "evidence_hijacking",
    }
    assert fit.reader_can_follow is False
    # "没评"与"读不懂"必须分得开：没评是 None，不是 False
    assert ContextFit().reader_can_follow is None
    assert all(value is None for value in ContextFit().model_dump().values())


def test_topic_context_rides_inside_the_evidence_pack():
    """容器是证据包，但 `format_evidence` 绝不印它——两段契约不同。"""
    from app.agent.prompts import format_evidence

    pack = real_pack()

    assert pack.context is REAL_CONTEXT
    assert "新型云，指的是最近被热议的一批云服务模式" not in format_evidence(pack)
    assert "f1" in format_evidence(pack)
