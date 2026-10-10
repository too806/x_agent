"""中文 X 帖子的**传播结构**写作策略。

这一组盯的不是"模型写得好不好"（那不可复现），而是三件能判定的事：

1. **结构契约本身**：三条候选走哪三条路、开头/结尾各有几种形式、
   证据形状怎么变成可执行的指令。这些定义在 `app/agent/post_structure.py`，
   由 Writer 与 Critic **读同一份**——两处各写一遍必然漂，而漂了不会报错。
2. **契约真的进了调用链**：新规则必须出现在发给模型的 prompt 里，
   而不是留在一份没人读的文档里。这一组直接调工具、读 prompt。
3. **硬约束没被放松**：140 是硬上限、60–140 只是写作偏好、
   没有数字时不许出现数字、第一人称经历一律不许编、内部编号不许泄漏。

断言分两类，刻意分开写：
- **契约文本**（prompt 里有没有这条要求）用逐字断言——这是本仓既有的做法，
  改模板会让它响，而它响的时候人是能看懂该改哪里的。
- **行为**（长度、门禁、证据形状）用输入输出断言，不依赖任何具体词句——
  换一个说法实现同样的行为，这些测试照样过。
"""

from app.agent.budget import load_budget
from app.agent.nodes.guardrail import evaluate as guardrail_evaluate
from app.agent.persona import load_persona
from app.agent.post_structure import (
    POST_STRUCTURE_VERSION,
    ROLE_LABELS,
    ROLE_ORDER,
    CandidateRole,
    EndingMove,
    OpeningMove,
    evidence_author_count,
    evidence_shape,
    render_candidate_roles,
    render_ending_moves,
    render_evidence_shape,
    render_opening_moves,
)
from app.agent.schemas import (
    AgentDecision,
    Angle,
    Candidate,
    ClaimScope,
    EvidenceFact,
    EvidencePack,
    ViewpointStance,
)
from app.agent.tools.candidates import make_candidates_tool
from app.agent.tools.critic import make_critic_tool
from app.style.gate import evaluate_content_gate
from fakes import (
    ScriptedLLMService,
    candidates_payload,
    candidate_ids_in,
    critic_payload,
)
from types import SimpleNamespace

PERSONA = load_persona()

SOURCE_ONE = "11111111-1111-1111-1111-111111111111"
SOURCE_TWO = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# 夹具：两种证据形状——有数字的、没数字的
# ---------------------------------------------------------------------------


def angle(claim: str = "这条路径走不通，别再往这边投钱了。") -> Angle:
    """一个正常的观点。`stance` 不是 strong，免得程序兜底把 accept 改判掉——
    这一组测的不是那条闸门（它由 `test_agent_tools.py` 盯着）。"""
    return Angle(
        angle_id="a1",
        claim=claim,
        reasoning="从证据里的两个动作推得",
        why_it_matters="关系到钱往哪儿去",
        evidence_ids=["f1"],
        risk="若成本结构变了则不成立",
        counterpoints=["也有人认为只是暂时的"],
        counterpoint_evidence_ids=[],
        assumptions=[],
        implications=[],
        claim_scope=ClaimScope.SEGMENT,
        stance=ViewpointStance.CAUTIOUS,
    )


def state_with(pack: EvidencePack) -> dict:
    return {
        "run_id": "run-structure",
        "goal": "看看这个方向该怎么判断",
        "selected_topic": "一个正在被讨论的选题",
        "selected_angle": angle(),
        "evidence_pack": pack,
        "research_item_ids": [SOURCE_ONE, SOURCE_TWO],
        "research_results": [
            SimpleNamespace(author_username="alice", content="材料一"),
            SimpleNamespace(author_username="bob", content="材料二"),
        ],
        "style_recipe": None,
        "writer_epoch": 1,
        "critic_epoch": 0,
    }


def pack_with_numbers() -> EvidencePack:
    """有可核验数字的证据：两位作者、两条来源、事实里带着一个数。"""
    return EvidencePack(
        facts=[
            EvidenceFact(
                fact_id="f1",
                fact="这家公司把入门价从每月 20 元降到了 9 元。",
                source_ids=[SOURCE_ONE],
            ),
            EvidenceFact(
                fact_id="f2",
                fact="同一周另一家把免费额度提高了一倍。",
                source_ids=[SOURCE_TWO],
            ),
        ],
        counterpoints=["有人认为这只是一次季度促销"],
        unknowns=["查不到两家的毛利"],
    )


def pack_without_numbers() -> EvidencePack:
    """没有可核验数字的证据：全是不带数字的判断与动作描述。"""
    return EvidencePack(
        facts=[
            EvidenceFact(
                fact_id="f1",
                fact="这家公司把入门套餐直接取消了。",
                source_ids=[SOURCE_ONE],
            ),
        ],
        counterpoints=["也有人认为只是渠道调整"],
        unknowns=["查不到替代方案"],
    )


async def writer_prompt(state: dict | None = None) -> str:
    llm = ScriptedLLMService(
        responses={"candidates": candidates_payload(content="正文。")}
    )
    tool = make_candidates_tool(llm, PERSONA, load_budget())
    await tool(
        state if state is not None else state_with(pack_with_numbers()),
        AgentDecision(action="generate_candidates", reason="写"),
    )
    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt  # 漏传的占位符不会报错，只会原样发给模型
    return prompt


async def critic_prompt(state: dict) -> str:
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept")})
    tool = make_critic_tool(llm, PERSONA)
    candidates = [
        Candidate(candidate_id=f"c{i}", label=ROLE_LABELS[role], content="正文。")
        for i, role in enumerate(ROLE_ORDER, start=1)
    ]
    await tool({**state, "candidates": candidates}, AgentDecision(action="criticize", reason="评"))
    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt
    return prompt


# ---------------------------------------------------------------------------
# 一、结构契约：三条路、开头形式、结尾形式
# ---------------------------------------------------------------------------


def test_the_three_roles_are_declared_once_and_are_distinct():
    """三条候选三条路，定义只有一处（工作单第四节）。

    三条同义改写等于只写了一条——而 Critic 面对三条同质候选也无从比较，
    它挑不出"哪条更适合这个角度"，只能挑"哪条措辞更顺"。所以角色集合本身
    是这一层的一等公民：名字、顺序、中文名都在这里定死。
    """
    assert len(ROLE_ORDER) == 3
    assert set(ROLE_LABELS) == set(ROLE_ORDER)
    # 中文名互不相同——重名会让"三条走了三条路"在报告里看不出来。
    assert len(set(ROLE_LABELS.values())) == 3
    assert set(ROLE_ORDER) == {
        CandidateRole.VERDICT,
        CandidateRole.FACT_CONTRAST,
        CandidateRole.DISCUSSION,
    }


def test_the_role_count_matches_the_writer_budget():
    """角色数必须等于一轮产出的候选数（工作单第四节）。

    加一条路而没同步 `candidates_per_round`，那条路就永远不会被写出来：
    工具按 `wanted` 截断，多出来的角色是个死定义。反过来把预算调大也是同样的问题。
    这条把"代码里的契约"和"配置里的预算"钉在一起——它们分属两个文件，
    漂开时不会有任何报错。
    """
    assert load_budget().candidates_per_round == len(ROLE_ORDER)


def test_the_structured_contract_is_versioned():
    """结构化契约有版本号，改动要进位（工作单第二节）。

    `POST_STRUCTURE_VERSION` 与 `app/style/schemas.py::FEATURE_VERSION`、
    `evals/__init__.py::__version__` 是同一条纪律：口径变了而版本没动，
    两份报告会被读成"稿子变了"。
    """
    assert POST_STRUCTURE_VERSION


def test_the_opening_and_ending_catalogues_carry_their_prohibitions():
    """开头/结尾的形式清单必须带着**禁用项**一起给（工作单第三节 A / E）。

    只给一份"可以怎么开头"的清单是不够的：模型会退化成同一个套路的三次措辞，
    或者用套话起头。禁项必须逐条出现在渲染结果里，否则它只是文档里的一句话。
    """
    openings = render_opening_moves()
    assert "至少用两种不同的形式" in openings
    for phrase in ("一个扎心的事实", "冷知识", "我发现", "真相令人震惊", "背后水很深"):
        assert phrase in openings
    # 钩子不是隐瞒事实——这一条与"不许套话"是两回事，必须各说各的。
    assert "隐瞒关键事实" in openings

    endings = render_ending_moves()
    assert "不要求每条都提问" in endings
    for phrase in ("你怎么看", "评论区见", "欢迎讨论", "点赞关注"):
        assert phrase in endings
    assert "乞求互动" in endings


def test_every_opening_and_ending_move_has_an_instruction():
    """每种形式都要有"什么时候用"，而不是只列一个名字。

    只给 `question`、`scene` 这样的名字，模型只能猜；给了适用条件，
    "生活化的选题别硬用鲜明判断开头"这类判断才落在它手上。
    """
    openings = render_opening_moves()
    endings = render_ending_moves()
    for move in OpeningMove:
        assert move.value in openings
    for move in EndingMove:
        assert move.value in endings


def test_the_role_rendering_carries_a_prohibition_per_route():
    """每条路都要带禁忌（工作单第四节）。

    最常见的退化方式是把三条候选写成"同一条 + 堆数字 + 加个问号"：
    第 2 条堆数字、第 3 条只是第 1 条加问号。禁忌逐条写出来才挡得住。
    """
    rendered = render_candidate_roles()
    for label in ROLE_LABELS.values():
        assert label in rendered
    assert "同义改写" in rendered or "换三次措辞" in rendered
    # 没有可靠数字时不许编——这条是硬约束，必须在事实型那一条里说。
    assert "编" in rendered


# ---------------------------------------------------------------------------
# 二、证据形状 → 可执行的写作指令（工作单第五、七节）
# ---------------------------------------------------------------------------


def test_the_shape_counts_facts_materials_and_authors():
    """形状是**数出来的**，不是模型判断的。

    `fact_count` 复用 `evidence_signals`（那里刻意只看事实正文，绝不用 repr
    兜底——`EvidenceFact` 的 repr 里带着 `fact_id='f1'`，那个 `1` 会让
    "有数字"对每一条事实都为真）。
    """
    shape = evidence_shape(pack_with_numbers(), author_count=2)

    assert shape.fact_count == 2
    # 两条事实各来自一条材料 → 两条材料。
    assert shape.material_count == 2
    assert shape.counterpoint_count == 1
    assert shape.author_count == 2
    assert shape.has_numeric_fact is True
    assert shape.has_facts is True


def test_a_single_material_is_visible_in_the_shape():
    """全部事实来自同一条材料时，形状要能看出这一点（工作单第五节）。

    "业内普遍认为"这类共识口径在单一来源上是无据的，而这只能从
    `material_count == 1` 看出来。
    """
    shape = evidence_shape(pack_without_numbers())

    assert shape.material_count == 1
    assert shape.fact_count == 1


def test_no_facts_renders_a_stop_signal():
    """一条事实都没有时，渲染的是"什么都别编"，不是"随便写"。"""
    rendered = render_evidence_shape(evidence_shape(EvidencePack()))

    assert "没有任何可用事实" in rendered
    assert "编" in rendered


def test_the_shape_permits_numbers_only_when_they_exist():
    """有数字 / 没数字是**两条不同的指令**，不能合成一句"使用具体数据"。

    合成一句话的代价是真机上的一个已知故障：模型在没有材料的那一轮照样
    长出一个"约 30%"。所以两个方向都要钉住。
    """
    with_numbers = render_evidence_shape(evidence_shape(pack_with_numbers()))
    without = render_evidence_shape(evidence_shape(pack_without_numbers()))

    # 有数字：可以用，但必须留住单位/口径，且不许把相关写成因果。
    assert "**有**可核验的数字" in with_numbers
    assert "不许" in with_numbers
    assert "不许出现具体的数字" not in with_numbers

    # 没数字：明确禁止出现任何具体数字、比例、金额、倍数、排名。
    assert "**没有**可核验的数字" in without
    assert "不许出现具体的数字" in without
    assert "宁可只讲道理" in without


def test_the_forbidden_thing_is_scoped_to_external_factual_numbers():
    """"不许出现数字"管的是**对外部世界的事实性量化断言**，不是数字字符本身。

    一场真实误杀：Critic 把「三个值得注意的问题」判成编造数字，一条结构完全
    正常的稿子被打回改稿，而改稿只会把「三个问题」改成「几个问题」——信息量
    少了，问题也没解决。所以"禁止"这句话必须把范围写出来。
    """
    without = render_evidence_shape(evidence_shape(pack_without_numbers()))

    assert "外部世界的事实性量化断言" in without
    # 三个典型的事实性量化断言要被点名，好让模型知道边界在哪。
    for example in ("股价", "收入", "百分比"):
        assert example in without


def test_the_no_numbers_shape_still_allows_structural_numbering():
    """"没有数字"的**反面不是"正文里不许出现数字字符"**。

    给选项编号、「三个问题」「二选一」「第一种做法」数的是正文里已经交代过的
    东西，不对世界做量化断言。这一条不写出来，模型在"不许出现数字"的压力下
    会把它们一起删掉——那是把一条正常的中文表达改坏。
    """
    without = render_evidence_shape(evidence_shape(pack_without_numbers()))

    assert "结构性表达" in without
    assert "三个值得注意的问题" in without
    assert "二选一" in without


def test_the_with_numbers_shape_allows_derived_computations():
    """材料**有**数字时，允许"口径一致、能从材料算回来"的推算。

    反过来的误杀同样真实：把"5 美元降到 3 美元，降了四成"判成编造，因为
    "四成"没在来源里原样出现。判据是**能不能算回来**，不是**有没有原样引用**。
    """
    with_numbers = render_evidence_shape(evidence_shape(pack_with_numbers()))

    assert "算回来" in with_numbers
    assert "算出来的数" in with_numbers





def test_a_single_material_changes_the_instruction_not_just_a_number():
    """同一条材料的事实不能写成"业内普遍认为"（工作单第五节）。

    只印一个数字 `1` 是没用的——模型不会自己去查那意味着什么。
    """
    rendered = render_evidence_shape(evidence_shape(pack_without_numbers()))

    assert "同 1 条材料" in rendered
    assert "业内普遍认为" in rendered


def test_the_first_person_red_line_is_always_rendered():
    """第一人称经历的红线在任何形状下都要出现（工作单第五节）。

    系统没有给作者任何关于他自己的材料——"我最近赚了""我实测过"一律是编造。
    这一条**不能**只在"没有事实"的分支里说：恰恰是有事实的那一轮，
    模型最容易顺手补一段个人经历来增加说服力。
    """
    for pack in (pack_with_numbers(), pack_without_numbers()):
        rendered = render_evidence_shape(evidence_shape(pack))
        assert "编造个人经历和编造数字是同一种错误" in rendered


def test_the_author_count_distinguishes_absent_from_zero():
    """`author_count` 的 None（没统计）与 0（数出来是 0）必须分得开。

    同 `evidence_signals` 与 `StyleFit` 的纪律：`None` 是"没量过"，
    0 是一句关于语料的结论。两者混起来，报告里会出现"作者 0 位"这种
    实际上没人算过的话。
    """
    assert evidence_shape(pack_with_numbers()).author_count is None
    assert evidence_shape(pack_with_numbers(), author_count=0).author_count == 0
    assert evidence_shape(None).author_count is None
    assert evidence_shape(None).fact_count == 0


def test_the_author_count_ignores_unused_materials():
    """只数**被引用**的材料作者，不数本轮抓回来的全部材料。

    用 `research_results` 的全体作者会让"3 位作者"变成一句比证据本身更宽的话。
    """
    state = state_with(pack_with_numbers())
    assert evidence_author_count(state) == 2

    # 把事实里的来源缩到一条 → 只剩一位作者。
    state["evidence_pack"] = EvidencePack(
        facts=[EvidenceFact(fact_id="f1", fact="只有一个动作。", source_ids=[SOURCE_ONE])]
    )
    assert evidence_author_count(state) == 1


# ---------------------------------------------------------------------------
# 三、契约真的进了 Writer / Critic 的调用链（工作单第十一节）
# ---------------------------------------------------------------------------


async def test_the_writer_gets_the_whole_structure_contract():
    """三条路、开头清单、结尾清单、证据形状都在 Writer 的 prompt 里。

    **这才是这一层的意义**：规则写在文档里等于没写。这条直接调工具、
    读它发给模型的 prompt——prompt 里没有，就等于没生效。
    """
    prompt = await writer_prompt()

    assert ROLE_LABELS[CandidateRole.VERDICT] in prompt
    assert ROLE_LABELS[CandidateRole.FACT_CONTRAST] in prompt
    assert ROLE_LABELS[CandidateRole.DISCUSSION] in prompt
    assert "至少用两种不同的形式" in prompt
    assert "不要求每条都提问" in prompt
    assert "本次证据的形状" in prompt


async def test_the_writer_is_told_not_to_fabricate_a_first_person_experience():
    """第一人称红线要在 Writer prompt 里（工作单第五节）。

    这是本次**没有**用程序门禁兜的一条（不像内部编号那样有一条正则），
    所以它只能靠 prompt 与 persona 两处同时说，且都必须被钉住。
    """
    prompt = await writer_prompt()

    assert "不许编造个人经历" in prompt
    assert "我最近赚了多少" in prompt
    assert "我亲眼看到" in prompt


async def test_the_writer_is_told_numbers_are_forbidden_without_evidence():
    """没有数字时，prompt 里必须是"不许出现数字"，而不是沉默。"""
    prompt = await writer_prompt(state_with(pack_without_numbers()))

    assert "**没有**可核验的数字" in prompt
    assert "不许出现具体的数字" in prompt


async def test_the_writer_is_told_the_evidence_really_has_numbers():
    """有数字时要走另一条分支——两条分支必须真的分开。

    注意不能拿"不许出现具体的数字"来判：persona 的事实红线里**永远**有这一句
    （它是无条件的）。有数字时才该消失的是**形状**里那条"本次材料里没有……"，
    所以判据只能是它。
    """
    prompt = await writer_prompt(state_with(pack_with_numbers()))

    assert "**没有**可核验的数字" not in prompt
    assert "**有**可核验的数字" in prompt


async def test_the_critic_sees_the_same_shape_and_the_same_roles():
    """Critic 拿到的是**同一个**形状与同一份角色定义。

    两边形状不一致时，Critic 会拿"没有数字"去判一条被明确要求用数字写的正文——
    而那不是报错，只是一份莫名其妙的评审意见。
    """
    state = {**state_with(pack_with_numbers())}
    prompt = await critic_prompt(state)

    assert "本次证据的形状" in prompt
    assert "**有**可核验的数字" in prompt
    assert ROLE_LABELS[CandidateRole.FACT_CONTRAST] in prompt


async def test_the_critic_is_told_a_candidate_with_no_numbers_is_fabricating():
    """没有数字的证据下，正文里的任何数字都是编造，且是 P0。"""
    state = {**state_with(pack_without_numbers())}
    prompt = await critic_prompt(state)

    assert "**没有**可核验的数字" in prompt
    # 不是"参考一下有没有数字"：形状说没有数字而正文里出现了，就是**编造**，
    # factual_support 直接 0 分并记 P0。
    assert "直接 0 分并记 P0" in prompt


async def test_the_writer_is_told_structural_numbering_is_allowed_without_evidence():
    """Writer 也要知道那条禁令**不管**结构性表达。

    只对 Critic 说、不对 Writer 说，会变成"作者不敢写、评委又不扣分"——
    结果是稿子主动丢掉「三个问题」这类正常表达，白白变差。
    """
    prompt = await writer_prompt(state_with(pack_without_numbers()))

    assert "结构性表达" in prompt
    assert "二选一" in prompt


async def test_the_critic_must_read_what_a_number_actually_is():
    """Critic 不许"见数字就归零"：它要先看清那个数字是哪一类。

    三类豁免必须逐类写出（结构表达 / 选项编号 / 有依据的推算），
    否则模型只会读成一句可以忽略的"注意别误判"。
    """
    prompt = await critic_prompt({**state_with(pack_without_numbers())})

    assert "结构性数字表达" in prompt
    assert "三个值得注意的问题" in prompt
    assert "A/B/C" in prompt
    assert "算回来" in prompt


async def test_the_critic_p0_is_scoped_to_real_fabrication():
    """P0 只给真正的严重造假：结构性编号不许被按 P0 处理。

    这一条与上面那条是两个层：上面说"这类数字不是造假"（factual_support 那一项），
    这里说"就算你要升级，也不许升到 P0"（优先级那一档）。模型很容易在前一处
    明白了、在后一处又把它归进"最严重"。而 P0 在流程里的含义是"必须处理"，
    误判会让一条正常稿子被反复打回，预算耗在一个不存在的问题上。
    """
    prompt = await critic_prompt({**state_with(pack_without_numbers())})

    assert "都不属于 P0" in prompt
    # 同时那条防漏判的话必须还在：真正的外部事实性数字仍然是 P0。
    assert "外部世界的事实性量化断言" in prompt


async def test_writer_and_critic_share_one_evidence_shape_contract():
    """**同一份**形状文本同时出现在 Writer 与 Critic 的 prompt 里。

    两边各渲染一份是本任务范围外最容易发生的漂移：形状改了、只改了一边，
    于是 Writer 被要求用数字写、Critic 被判"没有数字所以是编造"——那不是
    报错，是一份莫名其妙的评审意见，而它会把一条好稿子判成 P0。
    这里直接比对**逐字相同**的那段文本，而不是分别断言两处含某个词。
    """
    pack = pack_with_numbers()
    # `state_with` 的两条材料来自两位不同作者，所以形状里的作者数是 2
    # ——按生产代码那一条路算出期望文本，而不是自己另算一个。
    rendered = render_evidence_shape(evidence_shape(pack, author_count=2))

    writer = await writer_prompt(state_with(pack))
    critic = await critic_prompt(state_with(pack))

    assert "作者 2 位" in rendered
    assert rendered in writer
    assert rendered in critic


async def test_the_critic_is_told_distinctness_and_discussion_are_not_gates():
    """两个新维度都只是**观察**，不许变成改稿理由（工作单第六节）。

    这一条防的是真实的坏循环：改稿一次只能重写一条，"三条太像"判成 P1 会让
    每一轮都判 revise，预算空转两轮之后照样交付，而质量并不更好。
    """
    state = {**state_with(pack_with_numbers())}
    prompt = await critic_prompt(state)

    assert "candidate_distinctness" in prompt
    assert "discussion_potential" in prompt
    assert "不参与 decision" in prompt
    assert "不是门禁" in prompt


async def test_the_critic_is_told_what_not_to_mistake_for_quality():
    """四条"不要混"的判断要在 Critic prompt 里（工作单第六节）。

    没有这一段，评分会朝最省事的方向漂：长句判低、短句判高、有问号判高、
    有情绪判高。四条都是这一层明确要求挡住的。
    """
    state = {**state_with(pack_with_numbers())}
    prompt = await critic_prompt(state)

    assert "有争议 ≠ 高质量" in prompt
    assert "结尾有问号 ≠ 该加分" in prompt
    assert "句子短 ≠ 可读性好" in prompt
    assert "有数字 ≠ 信息价值高" in prompt


async def test_the_critic_reports_distinctness_without_gating_on_it():
    """差异度低只写进摘要，不改任何一条的结论。

    摘要会进 `decision_trace` 与 `agent_decision`，于是"这一批其实是一句话的
    三种措辞"在控制台看得见——而 `decision` 一个都不动。
    """

    def payload(prompt: str) -> str:
        import json

        cids = candidate_ids_in(prompt)
        return json.dumps(
            {
                "results": [
                    {
                        "candidate_id": cid,
                        "score": 8.0,
                        "flags": [],
                        "revision_targets": [],
                        "decision": "accept",
                    }
                    for cid in cids
                ],
                "focus_candidate_id": cids[0],
                "candidate_distinctness": 3.0,
                "distinctness_note": "三条都从同一个事实起步",
            },
            ensure_ascii=False,
        )

    llm = ScriptedLLMService(responses={"critic": payload})
    tool = make_critic_tool(llm, PERSONA)
    state = {
        **state_with(pack_with_numbers()),
        "candidates": [
            Candidate(candidate_id=f"c{i}", label=ROLE_LABELS[role], content="正文。")
            for i, role in enumerate(ROLE_ORDER, start=1)
        ],
    }

    result = await tool(state, AgentDecision(action="criticize", reason="评"))

    # 结论没被这一项动过：三条仍然 accept。
    assert {r.decision for r in result.delta["critic_results"]} == {"accept"}
    # 但观察出现在人看得到的地方。
    assert "差异度 3.0/10" in result.summary
    assert "三条都从同一个事实起步" in result.summary
    assert "不构成改稿理由" in result.summary


# ---------------------------------------------------------------------------
# 四、长度：推荐区间是**偏好**，硬上限才是校验（工作单第七节）
# ---------------------------------------------------------------------------


def test_the_recommended_range_is_a_preference_not_a_second_limit():
    """60–140 是推荐生成范围，140 才是硬上限。

    这条钉的是"没有第二个不可绕过的门禁"：推荐下沿比上限小、且是一个
    可以缺省的偏好值（老配置没有它时 Writer prompt 只说上限）。
    """
    assert PERSONA.max_length == 140
    assert PERSONA.recommended_min_length is not None
    assert 0 < PERSONA.recommended_min_length < PERSONA.max_length


def test_a_naturally_short_post_is_not_a_violation():
    """比推荐下沿短的干净稿子**不产生任何原因码**。

    这是"推荐范围没有变成门禁"的行为证据：不是断言 prompt 里怎么写，
    而是断言系统对一个短帖的实际判定——短不是问题。
    """
    short = "取消入门套餐这件事，真正被改变的是采购决策。"
    assert len(short) < PERSONA.recommended_min_length

    passed, reasons = guardrail_evaluate(short, max_length=PERSONA.max_length)

    assert passed is True
    assert reasons == []


def test_only_the_hard_ceiling_produces_too_long():
    """越界判定只看 140：140 字以内一个码都不多，141 才是 `too_long`。"""
    at_limit = "字" * PERSONA.max_length
    over = "字" * (PERSONA.max_length + 1)

    assert guardrail_evaluate(at_limit, max_length=PERSONA.max_length) == (True, [])
    passed, reasons = guardrail_evaluate(over, max_length=PERSONA.max_length)
    assert passed is False
    assert reasons == ["too_long"]


async def test_the_writer_is_given_both_ends_of_the_range():
    """区间两端都要印进 prompt：只给上限，模型会默认"越长越好"。"""
    prompt = await writer_prompt()

    assert str(PERSONA.recommended_min_length) in prompt
    assert str(PERSONA.max_length) in prompt
    assert "写作目标" in prompt


# ---------------------------------------------------------------------------
# 五、不泄漏内部信息（工作单第七节）
# ---------------------------------------------------------------------------


def test_internal_fact_ids_never_reach_the_reader():
    """正文里的内部编号会被发布门禁拦下——这一条是硬约束，不是建议。

    它的存在决定了 prompt 里那句"绝对不许出现内部事实编号"不是修辞。
    """
    leaked = "这家公司发布了 9 款产品（f1），值得注意。"
    violations = evaluate_content_gate(leaked, fact_ids=["f1", "f2"])

    assert [v.code for v in violations] == ["internal_ref_leak"]

    clean = "这家公司发布了 9 款产品，值得注意。"
    assert evaluate_content_gate(clean, fact_ids=["f1", "f2"]) == []


def test_the_writer_and_the_reviser_are_both_told_the_gate_is_real():
    """两处 prompt 都要说明"出现编号会被拦下"，否则改稿那一轮会把它加回来。"""
    from app.agent.prompts import load_prompt

    for name in ("candidates", "revise"):
        template = load_prompt(name)
        assert "编号" in template
        assert "拦" in template


# ---------------------------------------------------------------------------
# 六、没有副作用：观点强度上限与审核/发布路径不受影响
# ---------------------------------------------------------------------------


async def test_the_evidence_ceiling_still_overrules_an_accept():
    """这一次改动没有放松"观点 ≤ 证据"那条程序兜底（工作单第五节）。

    新增的字段都是**观察**（`discussion_potential` / `candidate_distinctness`，
    默认 `None`），因此一条被判定越界的 accept 仍然必须被改判成 revise。
    """
    from test_agent_tools import MOTIVE_CONTENT, scored_angle

    llm = ScriptedLLMService(responses={"critic": critic_payload("accept", score=9.0)})
    tool = make_critic_tool(llm, PERSONA)
    state = {
        "run_id": "run-1",
        "goal": "目标",
        "candidates": [
            Candidate(candidate_id=f"c{i}", label=ROLE_LABELS[role], content=MOTIVE_CONTENT)
            for i, role in enumerate(ROLE_ORDER, start=1)
        ],
        "selected_angle": scored_angle(bound_sources=1, bound_authors=1),
        "writer_epoch": 1,
        "critic_epoch": 0,
        "evidence_pack": EvidencePack(
            facts=[EvidenceFact(fact="发布了 9 款产品", source_ids=["s1"])]
        ),
    }

    delta = (
        await tool(state, AgentDecision(action="criticize", reason="评"))
    ).delta

    assert {r.decision for r in delta["critic_results"]} == {"revise"}
    assert "evidence_ceiling" in delta["critic_result"].flags
    # 分数没有被程序动过。
    assert {r.score for r in delta["critic_results"]} == {9.0}


async def test_a_full_run_still_stops_at_human_review(session_factory):
    """整条 Agent 链路跑到头仍然停在人工审核（工作单第十节）。

    这一层改的是**怎么写**，一个字都没动"谁来审、能不能发"。所以端到端的
    终点必须还是同一个：graph 不越过 `interrupt()`，不自动发布。
    这条是这一组里唯一一条真的跑图（其余的都在工具与纯函数这一级）。
    """
    from test_agent_loop import config_for, drive, happy_llm
    from support import runtime_with_checkpointer

    llm = happy_llm()
    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)

        assert "__interrupt__" in out
        payload = out["__interrupt__"][0].value
        assert payload["type"] == "content_review"
        # 交付的正文仍然是那条被评审接受的稿子，且**没有**被发布出去。
        assert out["draft_content"]
        assert len(out["draft_content"]) <= PERSONA.max_length

        snapshot = await runtime.graph.aget_state(config_for(run_id))
        assert snapshot.next == ("human_review",)


# ---------------------------------------------------------------------------
# 七、离线评价样例：六个中文题类别（工作单第九节）
# ---------------------------------------------------------------------------


def test_the_offline_chinese_topic_set_is_coherent():
    """固定中文选题 + 参考评价标准，**不联网、不调用 LLM**（工作单第九节）。

    它是给人看的评价样例，不是给生产 Writer 复制的模板——所以生产那边
    一行都不 import 它。这条只验"样例本身立得住"：六个类别、八条判据、
    引用的角色与开头形式都还在 `post_structure` 里。
    """
    from evals.content.topics import CASES, CRITERIA

    assert len(CASES) == 6
    assert len(CRITERIA) == 8
    for criterion in CRITERIA:
        assert criterion.fail_signal.strip()
    for case in CASES:
        assert case.goal.strip()
        assert case.risks
        assert case.fit_roles and set(case.fit_roles) <= set(ROLE_ORDER)
        assert set(case.fit_openings) <= set(OpeningMove)


def test_the_agent_development_case_calls_out_fabricated_experience():
    """「AI Agent 开发经历」这一类必须显式点出编造第一人称经历（工作单第五节）。

    这是最容易被"写得生动一点"带偏的一类：作者没有任何关于他自己的材料，
    所以这一段经历只能来自材料与目标，不能来自想象。
    """
    from evals.content.topics import CASES

    case = next(c for c in CASES if c.case_id == "T-AGENT-DEV")
    joined = "".join(case.risks)

    assert "编造个人经历" in joined
    assert "我刚上线" in joined or "我实测" in joined


def test_the_offline_topic_set_does_not_leak_into_production():
    """评估层只读生产代码，生产代码不 import 评估层（工作单第九节）。

    这条边界一旦破了，`app/` 就会依赖一份"评价标准"，而评价标准是会随认知
    变化的东西——它不该成为业务依赖。
    """
    import pathlib
    import re

    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r"^\s*(?:from|import)\s+evals\b", re.MULTILINE)
    offenders = sorted(
        str(path)
        for path in app_dir.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    )

    assert offenders == []
