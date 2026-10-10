"""X 帖子的写法约束（工作单第八～十一节）。

这一组测试盯的**不是**"模型写得好不好"——那不可复现。它盯的是**约束真的被
送到了模型面前**：这一条帖子要做什么、哪些写法算失手、以及那句自检
（删掉连接词之后判断还在不在）。

Run 20ab360c 交付的那条稿子是一篇新闻分析：背景、事实、共识、反方各占一句，
读者读完知道了很多，却不知道作者自己发现了什么。而当时的 prompt 里没有任何
一句在要求"一条帖子只讲一个点"——这一组就是补上那个缺口，并把它钉住。

第二节是确定性信号：总结腔的连接词现在能被数出来（`EMPTY_CONNECTIVES`），
于是"这一版的 AI 味是不是更重了"有数可查，而不是靠读起来的感觉。
（改那张词表要同时进位 `evals/__init__.py` 的版本号——这份测试也钉住这一点。）

断言里**不出现任何具体话题**：规范一旦退化成"某某话题要怎么写"，下一次换
题目就失效了，而那种失效不会报错。
"""

from types import SimpleNamespace

from app.agent.budget import AgentBudget
from app.agent.persona import load_persona
from app.agent.prompts import format_persona
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
from app.style import features
from app.style.signals import ai_style_signals, format_signals
from fakes import ScriptedLLMService, candidates_payload, critic_payload

SOURCE_ONE = "11111111-1111-1111-1111-111111111111"

PERSONA = load_persona()


def angle() -> Angle:
    return Angle(
        angle_id="a1",
        claim="这条路径走不通，别再往这边投钱了。",
        reasoning="从 f1 推得",
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


def writer_state() -> dict:
    return {
        "run_id": "run-style",
        "goal": "看看这个方向该怎么判断",
        "selected_topic": "一个正在被讨论的选题",
        "selected_angle": angle(),
        "evidence_pack": EvidencePack(
            facts=[
                EvidenceFact(
                    fact_id="f1",
                    fact="这家公司发布了 9 款产品。",
                    source_ids=[SOURCE_ONE],
                )
            ],
            counterpoints=["有人认为这只是常规更新"],
            unknowns=["查不到定价"],
        ),
        "research_item_ids": [SOURCE_ONE],
        "research_results": [
            SimpleNamespace(author_username="alice", content="材料正文")
        ],
        "style_recipe": None,
        "writer_epoch": 1,
        "critic_epoch": 0,
    }


async def writer_prompt() -> str:
    llm = ScriptedLLMService(
        responses={"candidates": candidates_payload(content="正文。")}
    )
    tool = make_candidates_tool(llm, PERSONA, AgentBudget())
    await tool(writer_state(), AgentDecision(action="generate_candidates", reason="写"))
    prompt = llm.calls[0]["prompt"]
    assert "{{" not in prompt  # 漏传的占位符不会报错，只会原样发出去
    return prompt


# ---------------------------------------------------------------------------
# 一、写作目标：一条帖子 = 一条事实 + 一个观察 + 一个判断
# ---------------------------------------------------------------------------


async def test_the_writer_is_told_what_a_post_is_made_of():
    """三样东西各是什么、以及判断**只留一个**（工作单第八、九节）。

    "只留一个"是这一条的关键：一篇里两个判断，读者读完不知道作者主张哪一个，
    而那正是"把研究报告压成一条"最常见的形态。
    """
    prompt = await writer_prompt()

    assert "一条帖子由三样东西组成" in prompt
    for fragment in ("**一条事实**", "**一个观察**", "**一个判断**"):
        assert fragment in prompt
    assert "**只留一个**" in prompt
    # "这是你自己加的那一层"——观察必须被说成作者的东西，不是证据的复述。
    assert "你自己加的那一层" in prompt


async def test_the_writer_is_told_a_post_is_not_a_compressed_report():
    """反面清单必须在 prompt 里（工作单第八、九节）。

    四种失手形态逐条出现：压成摘要、堆背景、重复事实、硬凑结论句。
    只写"要像人说话"是不够的——那不是可执行的约束。
    """
    prompt = await writer_prompt()

    assert "是研究报告摘要，不是帖子" in prompt
    assert "堆背景" in prompt
    assert "重复同一条事实" in prompt
    assert "硬凑结论句" in prompt
    # 没有新判断就别写那句总结——这是"少写一句"的许可，模型需要它。
    assert "没有新判断就别写这句" in prompt


async def test_the_writer_gets_a_self_check_for_summary_voice():
    """说人话不是禁用词表，是一句可操作的自检（工作单第十节）。

    禁用词表会让模型换一个同义词继续总结；"把连接词删掉，判断还在不在"
    检验的是那句话本身有没有信息。
    """
    prompt = await writer_prompt()

    assert "把这些连接词全删掉，剩下的判断还在不在？" in prompt
    assert "该删的是那句话，不是换一个同义词" in prompt


async def test_motive_must_be_written_as_a_reading():
    """动机/意图只能写成解读（工作单第六、十一节）。

    材料证实得了"做了什么"，证实不了"为了什么"。这一条与
    `viewpoint.accept_ceiling_for` 是同一件事的两面：prompt 让模型自己写对，
    程序兜住写错的那些。
    """
    prompt = await writer_prompt()

    assert "只能写成解读，不能写成事实" in prompt
    assert "编造公司动机" in prompt
    assert "评审判 accept 也会被程序改判成 revise" in prompt


async def test_the_length_rule_stays_a_target_not_a_platform_limit():
    """长度是**写作目标**，不是平台的硬限制（这个账号是 Premium）。

    prompt 里必须带上 persona 的真实上限，而不是任何写死的数字：上限是可以
    被改的（`Persona.max_length`），改完之后 prompt 还念旧数就会让模型照着
    一个不存在的限制写字。
    """
    prompt = await writer_prompt()

    assert str(PERSONA.max_length) in prompt
    assert "**写作目标**，不是平台" in prompt


async def test_the_persona_voice_carries_the_same_rules():
    """同一套要求在阅读契约里也要有（`config/persona.yaml`）。

    persona 是跨步骤共享的那份"怎么说话"：Writer、Critic、Revise 都拿到它。
    只写在 candidates 模板里的话，改稿那一轮就看不到这些要求了。
    """
    rendered = format_persona(PERSONA)

    assert "一条帖子只讲一个点" in rendered
    assert "帖子不是压缩过的研究报告" in rendered
    assert "不许编造动机、意图、目的、内幕消息、资本用意、真实原因" in rendered


# ---------------------------------------------------------------------------
# 二、Critic：像研究报告摘要属于 P1（工作单第七节）
# ---------------------------------------------------------------------------


async def test_the_critic_is_told_a_report_summary_is_a_p1():
    """判据与反例都要写清楚，否则每一次都判 revise（工作单第七节）。"""
    llm = ScriptedLLMService(responses={"critic": critic_payload("accept")})
    tool = make_critic_tool(llm, PERSONA)
    state = {
        **writer_state(),
        "candidates": [
            Candidate(candidate_id="c1", label="直接观点", content="正文。")
        ],
    }

    await tool(state, AgentDecision(action="criticize", reason="评"))

    prompt = llm.calls[0]["prompt"]
    assert "像研究报告摘要，不像帖子" in prompt
    # 反例：不许因为"信息多"就判它像摘要——那会让事实扎实的稿子被判死。
    assert '不是"信息多"' in prompt
    assert "摘要的特征是**没有作者的那一层观察**" in prompt


# ---------------------------------------------------------------------------
# 三、总结腔能被数出来（确定性信号）
# ---------------------------------------------------------------------------


def test_summary_connectives_are_counted_as_an_empty_connective():
    """「这意味着」这类词进的是**参考信号**，不是门禁。

    它们与「众所周知」那种假权威分开：一个是"把话包成结论"，一个是"把话说成
    共识"。混在一张表里，"注意到自己没在推进判断"这条信号会被"引用了权威"
    淹没。
    """
    for phrase in ("这意味着", "本质上", "换句话说", "可以看出"):
        assert phrase in features.EMPTY_CONNECTIVES
        signals = ai_style_signals(f"先给一条事实。{phrase}结论就是这样。")
        assert signals.empty_connective_count == 1


def test_the_style_signal_says_nothing_when_the_writing_is_clean():
    """没命中就不报——信号里出现"0 处"会让模型把"检查过"读成"有问题"。"""
    signals = ai_style_signals("这家公司发布了 9 款产品。产品本身没有新东西。")

    assert signals.empty_connective_count == 0
    assert "空洞连接词" not in format_signals(signals)


def test_the_eval_version_moved_with_the_word_list():
    """改词表要同时进位 `evals/__init__.py` 的版本号。

    `empty_connective_count` 的口径变了，两份 evaluation JSON 不进位就会被
    `compare.py` 当成"Agent 的 AI 味变重了"——那是这类工具最典型的假阳性。
    """
    import evals

    assert evals.__version__ == "1.2.0"
