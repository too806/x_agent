"""Context 在流水线上的接线：Evidence 产出 → Angle / Writer / Critic / Revise 使用。

工作单第十八节要的 Writer 六条与 Critic 四条都在这里，另外钉住两件容易退化的接线：

1. **`{{CONTEXT}}` 真的被替换了**。`render()` 只替换传进去的键，漏传不会报错——
   字面量会原样进入发给模型的 prompt。所以每条新块都断言 `"{{" not in prompt`。
2. **没有背景时不留空洞**。老 checkpoint、材料不足的运行都走这条路：
   渲染出来必须是一句交代，不是空串，也不能让 Writer 自己去补一段。

真机那份把"2.2 万星的开源库"当入口的草稿在 `tests/test_agent_context.py` 里，
这里管的是它**怎么被送进 Critic 的 prompt**。
"""

import json
from types import SimpleNamespace

from app.agent.budget import AgentBudget
from app.agent.persona import load_persona
from app.agent.schemas import (
    AgentDecision,
    Angle,
    Candidate,
    ClaimScope,
    ContextOrigin,
    EvidenceFact,
    EvidencePack,
    TopicContext,
    ViewpointStance,
)
from app.agent.tools.angles import make_angles_tool
from app.agent.tools.candidates import make_candidates_tool
from app.agent.tools.critic import make_critic_tool
from app.agent.tools.evidence import make_evidence_tool
from app.agent.tools.revise import make_revise_tool
from fakes import (
    ScriptedLLMService,
    angles_payload,
    candidates_payload,
    critic_payload,
    evidence_payload_with_context,
    revise_payload,
    source_ids_in,
)

from test_agent_context import REAL_CONTEXT, REAL_DRAFT, REAL_FACT

SOURCE_ONE = "11111111-1111-1111-1111-111111111111"
SOURCE_TWO = "22222222-2222-2222-2222-222222222222"

PACK = EvidencePack(
    facts=[EvidenceFact(fact_id="f1", fact=REAL_FACT, source_ids=[SOURCE_ONE])],
    context=REAL_CONTEXT,
)


def tool_state(**extra) -> dict:
    state = {
        "run_id": "run-context",
        "goal": "今天关注美股，找一个能讨论的主题",
        "selected_topic": "华尔街热议的「新型云」到底是什么",
        "evidence_pack": PACK,
        "research_item_ids": [SOURCE_ONE],
        "research_results": [SimpleNamespace(author_username="alice", content="材料正文")],
        "writer_epoch": 1,
        "critic_epoch": 0,
    }
    state.update(extra)
    return state


def candidates_pool() -> list[Candidate]:
    return [Candidate(candidate_id="c1", label="直接观点", content=REAL_DRAFT)]


def angle() -> Angle:
    return Angle(
        angle_id="a1",
        claim="只卖算力的云，估值逻辑站不稳。",
        reasoning="从 f1 推得",
        why_it_matters="关系到这类云能不能被当作云厂商看",
        evidence_ids=["f1"],
        risk="算力单价回升则不成立",
        counterpoints=["降价可能只是促销"],
        counterpoint_evidence_ids=[],
        assumptions=[],
        implications=[],
        claim_scope=ClaimScope.SEGMENT,
        stance=ViewpointStance.CAUTIOUS,
    )


# ---------------------------------------------------------------------------
# Writer：六条（工作单第八节）
# ---------------------------------------------------------------------------


async def test_writer_gets_the_background_block():
    llm = ScriptedLLMService(responses={"candidates": candidates_payload(content=REAL_DRAFT)})
    tool = make_candidates_tool(llm, load_persona(), AgentBudget())

    await tool(tool_state(selected_angle=angle()), AgentDecision(action="generate_candidates", reason="写"))

    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt
    for fragment in (
        "新型云，指的是最近被热议的一批云服务模式",
        "去中心化 GPU 云是其中一类",
        "算力价格与模型成本同时下降",
        "只卖算力的云",
    ):
        assert fragment in prompt


async def test_writer_is_told_the_reader_does_not_know_the_topic():
    llm = ScriptedLLMService(responses={"candidates": candidates_payload(content=REAL_DRAFT)})
    tool = make_candidates_tool(llm, load_persona(), AgentBudget())

    await tool(tool_state(selected_angle=angle()), AgentDecision(action="generate_candidates", reason="写"))

    prompt = llm.calls[0]["prompt"]
    assert "读者不知道这个话题" in prompt
    assert "第一次听说这个话题" in prompt


async def test_writer_is_told_not_to_write_a_press_release_or_explainer():
    """背景是给读者的台阶，不是文章的主体（工作单第八节）。"""
    llm = ScriptedLLMService(responses={"candidates": candidates_payload(content=REAL_DRAFT)})
    tool = make_candidates_tool(llm, load_persona(), AgentBudget())

    await tool(tool_state(selected_angle=angle()), AgentDecision(action="generate_candidates", reason="写"))

    prompt = llm.calls[0]["prompt"]
    assert "新闻稿" in prompt and "百科" in prompt and "科普" in prompt


async def test_writer_is_told_not_to_use_a_fact_as_the_entry():
    """工作单第十二节：把真机那个反面例子写进 prompt，而不是只说"别跳步"。"""
    llm = ScriptedLLMService(responses={"candidates": candidates_payload(content=REAL_DRAFT)})
    tool = make_candidates_tool(llm, load_persona(), AgentBudget())

    await tool(tool_state(selected_angle=angle()), AgentDecision(action="generate_candidates", reason="写"))

    prompt = llm.calls[0]["prompt"]
    assert "不许拿一条事实当话题的入口" in prompt
    assert "2.2 万星" in prompt  # 反面例子必须具体到能被认出来
    assert "众所周知" in prompt  # 也不许假设读者"应该知道"


async def test_writer_is_not_told_to_dumb_down_vocabulary():
    """工作单第十一节：术语本身不是问题——判的是"有没有交代"，不是"够不够通俗"。"""
    llm = ScriptedLLMService(responses={"candidates": candidates_payload(content=REAL_DRAFT)})
    tool = make_candidates_tool(llm, load_persona(), AgentBudget())

    await tool(tool_state(selected_angle=angle()), AgentDecision(action="generate_candidates", reason="写"))

    prompt = llm.calls[0]["prompt"]
    assert "术语本身不是问题，没有交代才是问题" in prompt


async def test_writer_without_a_context_is_told_not_to_invent_one():
    """没有背景时也要有话说：一句交代 + 明确禁止自己补一段。

    少了这句，模型会把它知道的东西当成"这段素材"写进去——而那是**不可见**的：
    它既不在 state 里，也不在 Run Detail 里，事后无法解释稿子为什么长成那样。
    """
    pack = EvidencePack(facts=[EvidenceFact(fact_id="f1", fact=REAL_FACT, source_ids=[SOURCE_ONE])])
    llm = ScriptedLLMService(responses={"candidates": candidates_payload(content=REAL_DRAFT)})
    tool = make_candidates_tool(llm, load_persona(), AgentBudget())

    await tool(
        tool_state(evidence_pack=pack, selected_angle=angle()),
        AgentDecision(action="generate_candidates", reason="写"),
    )

    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt
    assert "没有可用的话题背景" in prompt
    assert "不要为了补背景去编" in prompt


# ---------------------------------------------------------------------------
# Critic：五条（工作单第十、十一、十四节）
# ---------------------------------------------------------------------------


async def test_critic_gets_the_same_background_as_the_writer():
    """两边拿到的必须是**同一份**渲染，否则 Critic 会拿另一段背景去判读者能不能进来。"""
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept")})
    tool = make_critic_tool(llm, load_persona())

    await tool(
        tool_state(candidates=candidates_pool(), selected_angle=angle()),
        AgentDecision(action="criticize", reason="评"),
    )

    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt
    assert "去中心化 GPU 云是其中一类" in prompt
    assert "新型云，指的是最近被热议的一批云服务模式" in prompt


async def test_critic_is_asked_for_the_reader_gate_and_three_reference_scores():
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept")})
    tool = make_critic_tool(llm, load_persona())

    await tool(
        tool_state(candidates=candidates_pool(), selected_angle=angle()),
        AgentDecision(action="criticize", reason="评"),
    )

    prompt = llm.calls[0]["prompt"]
    for field in ("reader_can_follow", "context_missing", "concept_jump", "evidence_hijacking"):
        assert field in prompt
    # 闸门是**是非题**：必须写明只收 true/false，否则模型会照旧给一个中间分放行
    assert "只能给 true 或 false" in prompt
    assert "`reader_can_follow=false` → **P1，必须处理**" in prompt
    # 真机那条"术语抄满背景、一个都没解释"的稿子正是靠**字面重合**骗过了旧信号，
    # 所以"重合不算进门"这句必须在，否则同一个故障会原样重演
    assert "词汇重合不是进门的证据" in prompt
    # 三个参考分的方向也要写清楚：越高越差，写反了整组就没意义
    assert "context_missing：**越高越差**" in prompt
    assert "evidence_hijacking：**越高越差**" in prompt


async def test_critic_is_told_that_a_blocked_reader_is_a_p1():
    """工作单第十四节：不能让 Critic 给这种内容一个高 Context Fit。"""
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept")})
    tool = make_critic_tool(llm, load_persona())

    await tool(
        tool_state(candidates=candidates_pool(), selected_angle=angle()),
        AgentDecision(action="criticize", reason="评"),
    )

    prompt = llm.calls[0]["prompt"]
    assert "把一条事实当成话题的入口" in prompt
    assert "同样是 P1" in prompt
    # 闸门本身也必须写明"false 即 P1"——否则它只是一个被记录下来的字段
    assert "reader_can_follow=false" in prompt


async def test_critic_is_told_not_to_penalise_specialist_vocabulary():
    """工作单第十一节：不要因为出现了专业名词就扣分。"""
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept")})
    tool = make_critic_tool(llm, load_persona())

    await tool(
        tool_state(candidates=candidates_pool(), selected_angle=angle()),
        AgentDecision(action="criticize", reason="评"),
    )

    prompt = llm.calls[0]["prompt"]
    assert "不要因为出现了专业名词就扣分" in prompt
    assert "不要要求固定写法" in prompt


async def test_critic_sees_the_deterministic_hit_for_the_real_draft():
    """确定性信号必须落在 prompt 里——它是 Critic 判断的唯一抓手。

    这一段是**参考事实**，不是分数：`format_context_signals` 由程序算出，
    不掺进 `context_fit`（同 AI 味信号那条纪律）。
    """
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept")})
    tool = make_critic_tool(llm, load_persona())

    await tool(
        tool_state(candidates=candidates_pool(), selected_angle=angle()),
        AgentDecision(action="criticize", reason="评"),
    )

    prompt = llm.calls[0]["prompt"]
    assert "[c1]" in prompt
    assert "2.2 万" in prompt
    assert "f1" in prompt
    assert "不是分数" in prompt


async def test_context_fit_survives_the_critic_tool():
    """新分项必须真的落到 state 上，否则它只是一个 prompt 里的摆设。"""
    fit = {
        "reader_can_follow": False,
        "context_missing": 6.0,
        "concept_jump": 9.0,
        "evidence_hijacking": 9.0,
    }
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept", context_fit=fit)})
    tool = make_critic_tool(llm, load_persona())

    delta = (
        await tool(
            tool_state(candidates=candidates_pool(), selected_angle=angle()),
            AgentDecision(action="criticize", reason="评"),
        )
    ).delta

    verdict = delta["critic_result"]
    assert verdict.context_fit.concept_jump == 9.0
    assert verdict.context_fit.reader_can_follow is False
    # 与 viewpoint_fit / style_fit 一样：没给就是 None，不是 0
    assert verdict.viewpoint_fit is None


async def test_critic_without_a_context_says_so_instead_of_leaving_a_hole():
    pack = EvidencePack(facts=[EvidenceFact(fact_id="f1", fact=REAL_FACT, source_ids=[SOURCE_ONE])])
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept")})
    tool = make_critic_tool(llm, load_persona())

    await tool(
        tool_state(evidence_pack=pack, candidates=candidates_pool(), selected_angle=angle()),
        AgentDecision(action="criticize", reason="评"),
    )

    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt
    assert "没有可用的话题背景" in prompt
    assert "没有话题背景可参照" in prompt


# ---------------------------------------------------------------------------
# Angle / Revise：同一条流水线的前后两端
# ---------------------------------------------------------------------------


async def test_angles_get_the_background_without_getting_a_conclusion():
    """工作单第七节：Background ≠ Angle。给 Angle 的那份要堵住"从背景推结论"。"""
    llm = ScriptedLLMService(responses={"angles": angles_payload()})
    tool = make_angles_tool(llm)

    await tool(tool_state(), AgentDecision(action="generate_angles", reason="想"))

    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt
    assert "去中心化 GPU 云是其中一类" in prompt
    assert "不是判断" in prompt
    # 反面例子也要在：背景没有事实编号，判断必须落在 evidence_ids 上
    assert "背景没有编号" in prompt


async def test_revise_gets_the_background_too():
    """改稿同样是写作：Critic 说了"读者进不来"，改稿这一步必须看得到该往哪补。"""
    from app.agent.schemas import CriticResult

    verdict = CriticResult(candidate_id="c1", decision="revise", score=4.0, revision_targets=["补背景"])
    llm = ScriptedLLMService(responses={"revise": revise_payload("改好的正文。")})
    tool = make_revise_tool(llm, load_persona())

    await tool(
        tool_state(
            candidates=candidates_pool(),
            selected_angle=angle(),
            critic_result=verdict,
            critic_results=[verdict],
        ),
        AgentDecision(action="revise", reason="改", target="c1"),
    )

    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt
    assert "去中心化 GPU 云是其中一类" in prompt
    assert "读者不知道这个话题" in prompt


# ---------------------------------------------------------------------------
# Evidence：背景与事实同一趟产出，且**同样要过校验**
# ---------------------------------------------------------------------------


async def test_evidence_tool_keeps_a_verified_background():
    payload = evidence_payload_with_context(
        {
            "subject": "新型云指最近被热议的一批云服务模式",
            "definition": "去中心化 GPU 云是其中一类",
            "why_now": "算力价格下降",
            "core_tension": "估值逻辑不同",
            "origin": "fact",
            "source_ids": [SOURCE_ONE],
        }
    )
    llm = ScriptedLLMService(responses={"evidence": payload})
    tool = make_evidence_tool(llm)

    result = await tool(tool_state(), AgentDecision(action="extract_evidence", reason="提炼"))

    context = result.delta["evidence_pack"].context
    assert context is not None
    assert context.origin is ContextOrigin.FACT
    assert context.source_ids == [SOURCE_ONE]
    assert "已形成话题背景" in result.summary


async def test_evidence_tool_never_lets_an_unsourced_claim_into_the_background():
    """模型把市场规模写进背景，工具必须把它拿掉——**并说出来**。"""
    payload = evidence_payload_with_context(
        {
            "subject": "这个市场很热。市场规模会到 3000 亿。",
            "definition": "",
            "why_now": "",
            "core_tension": "",
            "origin": "inference",
            "source_ids": [],
        }
    )
    llm = ScriptedLLMService(responses={"evidence": payload})
    tool = make_evidence_tool(llm)

    result = await tool(tool_state(), AgentDecision(action="extract_evidence", reason="提炼"))

    context = result.delta["evidence_pack"].context
    assert "3000 亿" not in context.subject
    assert context.subject == "这个市场很热。"
    # 修剪可见：summary 里必须有痕迹，"模型有多常在编背景"才看得见
    assert "背景修剪" in result.summary


async def test_evidence_tool_drops_the_background_entirely_when_nothing_survives():
    payload = evidence_payload_with_context(
        {"subject": "市场规模会到 3000 亿。", "origin": "inference"}
    )
    llm = ScriptedLLMService(responses={"evidence": payload})
    tool = make_evidence_tool(llm)

    result = await tool(tool_state(), AgentDecision(action="extract_evidence", reason="提炼"))

    assert result.delta["evidence_pack"].context is None
    assert "未能形成话题背景" in result.summary


async def test_evidence_tool_handles_a_model_that_omits_the_background():
    """漏字段不是错误路径：包照常产出，只是这一次没有背景。"""
    from fakes import evidence_payload

    llm = ScriptedLLMService(responses={"evidence": evidence_payload()})
    tool = make_evidence_tool(llm)

    result = await tool(tool_state(), AgentDecision(action="extract_evidence", reason="提炼"))

    assert result.delta["evidence_pack"].context is None
    assert "未能形成话题背景" in result.summary


# ---------------------------------------------------------------------------
# 事实条数 ≠ 独立支撑（工作单第五节）
# ---------------------------------------------------------------------------


def facts_payload(count: int, *, split: bool = False):
    """一份"每一条事实都追溯得到真实材料"的返回体。

    `split=True` 时事实按材料摊开（每条各引一条），否则全部引第一条——
    后者正是 Run 20ab360c 的形状：6 条事实全部来自同一篇帖子。
    """

    def build(prompt: str) -> str:
        ids = source_ids_in(prompt)
        return json.dumps(
            {
                "facts": [
                    {
                        "fact": f"同一批材料里拆出来的第 {i} 条事实",
                        "source_ids": [ids[(i - 1) % len(ids)]] if split else ids[:1],
                    }
                    for i in range(1, count + 1)
                ],
                "consensus": [],
                "counterpoints": [],
                "unknowns": [],
            },
            ensure_ascii=False,
        )

    return build


async def test_the_evidence_summary_never_lets_six_facts_read_as_six_sources():
    """6 条事实来自 1 条帖子，必须写成"独立支撑 1 条"。

    这是工作单第五节要分开的两个数。原先 summary 只有"形成 6 条可追溯事实"，
    而下游（模型、报告、人）读到的都是"有 6 份证据在托着同一个判断"——
    这正是 Run 20ab360c 里"1 篇帖子 + 若干事实 → 一个强烈的公司动机判断"的起点。
    """
    llm = ScriptedLLMService(responses={"evidence": facts_payload(6)})
    tool = make_evidence_tool(llm)

    result = await tool(tool_state(), AgentDecision(action="extract_evidence", reason="提炼"))

    assert len(result.delta["evidence_pack"].facts) == 6
    assert "6 条可追溯事实" in result.summary
    assert "独立支撑 1 条（1 条材料 / 1 位作者）" in result.summary


async def test_independent_support_needs_a_second_author_not_just_a_second_material():
    """两条材料、同一位作者 → 独立支撑仍然是 1。

    独立支撑取 min(材料数, 作者数)：同一个人的两条帖子不是第二份独立来源。
    只看材料数的话，"自己转自己"就能把独立支撑刷上去，而这一栏是
    `viewpoint.accept_ceiling_for` 判动机断言时的依据。
    """
    state = {
        **tool_state(),
        "research_item_ids": [SOURCE_ONE, SOURCE_TWO],
        "research_results": [
            SimpleNamespace(author_username="alice", content="材料一"),
            SimpleNamespace(author_username="alice", content="材料二"),
        ],
    }
    llm = ScriptedLLMService(responses={"evidence": facts_payload(4, split=True)})
    tool = make_evidence_tool(llm)

    result = await tool(state, AgentDecision(action="extract_evidence", reason="提炼"))

    assert "独立支撑 1 条（2 条材料 / 1 位作者）" in result.summary

    # 换成两位不同作者 → 独立支撑才变成 2。
    state["research_results"][1] = SimpleNamespace(author_username="bob", content="材料二")
    result = await tool(state, AgentDecision(action="extract_evidence", reason="提炼"))

    assert "独立支撑 2 条（2 条材料 / 2 位作者）" in result.summary


def test_rendered_evidence_states_the_material_count_next_to_the_fact_count():
    """印给模型看的证据包也要带上材料条数（工作单第五节）。

    模型是"把 6 条事实读成 6 份证据"的那一环，只修 summary 是不够的：
    它写观点、写正文时看到的是这一段渲染。
    """
    from app.agent.prompts import format_evidence

    pack = EvidencePack(
        facts=[
            EvidenceFact(fact_id=f"f{i}", fact=f"事实 {i}", source_ids=[SOURCE_ONE])
            for i in range(1, 7)
        ]
    )

    rendered = format_evidence(pack)

    assert rendered.startswith("（共 6 条事实，来自 1 条材料）")
    assert "f1" in rendered
