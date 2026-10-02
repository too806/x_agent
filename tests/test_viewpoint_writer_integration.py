"""观点 → Writer / Critic 的端到端行为（Phase 5B.3）。

这一层要证明的是三条**跨模块**的承诺，各自模块的单测都证明不了：

1. **Writer 与 Critic 拿到的是同一份渲染**。分开写两份的话，Critic 会拿一个
   与 Writer 不同的目标去判"有没有写对"，而两边都自认为正确——
   Phase 5B.2 的 `{{STYLE}}` 已经踩过一次这个坑，所以这里按同一套办法测：
   直接算出那份文本，然后去**真实发出去的 prompt** 里找它。
2. **正文忠于它该表达的那个判断**这件事是可追溯的：候选身上的
   `viewpoint_ref` 指向选定的 `angle_id`，并且改稿之后仍然指着同一个。
   没有这一条，Critic 的 `viewpoint_consistency` 就没有依据可判。
3. **分数不决定怎么写**（工作单第二十五节）：观点分与置信度一个都不进
   Writer 的 prompt。它们决定"选哪一个"，而"怎么写"由 StyleRecipe 决定。
"""

from __future__ import annotations

from app.agent.prompts import format_scope_signals, format_viewpoint
from fakes import critic_payload, revise_payload
from support import runtime_with_checkpointer
from test_agent_loop import decision, drive, happy_llm


def prompts_by_kind(llm) -> dict[str, str]:
    """假 LLM 记录下来的 prompt，按步骤归类。

    与 `test_style_writer_integration.prompts_by_kind` 同名同形：两处断言的
    都是"真实发出去的那段文本"，而不是"函数返回得对"。
    """
    return {call["kind"]: call["prompt"] for call in llm.calls}


def revision_path(*, critic=None, revise=None):
    """一条含**一轮改稿**的完整路径，用来验证改稿前后 viewpoint 都不丢。"""
    llm = happy_llm(
        critic=critic
        or [
            critic_payload("revise", focus="c1", score=4.0),
            critic_payload("accept", score=9.0),
        ],
        revise=revise or revise_payload("改好的正文：先把判断放在最前面。"),
    )
    llm.decisions = (
        decision("discover_topics"),
        decision("select_topic", target="t1"),
        decision("research_topic"),
        decision("extract_evidence"),
        decision("generate_angles"),
        decision("select_angle", target="a1"),
        decision("generate_candidates"),
        decision("criticize"),
        decision("revise"),
        decision("criticize"),
        decision("finish"),
    )
    return llm


# ---------------------------------------------------------------------------
# 同一份渲染，两个读者
# ---------------------------------------------------------------------------


async def test_writer_and_critic_receive_the_same_rendered_viewpoint(session_factory):
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    prompts = prompts_by_kind(llm)
    rendered = format_viewpoint(out["selected_angle"])
    assert rendered in prompts["candidates"]
    assert rendered in prompts["critic"]


async def test_the_rendered_viewpoint_is_the_judgement_with_its_boundary(session_factory):
    """给 Writer 的不是一句话，而是**判断 + 推理 + 依据 + 反方 + 边界**。

    这条防的是"升级了模型、忘了升级渲染"：字段都存下来了，prompt 里却还是
    只有 claim。断言的是真实 prompt 里那几行标题，不是 `format_viewpoint` 的
    返回值——后者无论调用方怎么用都会通过。
    """
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    angle = out["selected_angle"]
    writer_prompt = prompts_by_kind(llm)["candidates"]
    for line in (
        f"**判断**：{angle.claim}",
        f"**推理**：{angle.reasoning}",
        f"**依据**：{'、'.join(angle.evidence_ids)}",
        "**反方**：",
        "**边界**：",
    ):
        assert line in writer_prompt, line


async def test_the_writer_is_not_told_the_scores(session_factory):
    """分数决定"选哪一个"，不决定"怎么写"（工作单第二十五节）。

    印进 prompt 的话，模型会去迎合分数，而分数衡量的是证据结构不是措辞——
    迎合的结果是正文重复事实以显得"支撑充分"，那正是 novelty 在盯的反面。
    """
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    assert out["selected_angle"].viewpoint_score is not None  # 分数真的算出来了
    writer_prompt = prompts_by_kind(llm)["candidates"]
    assert "viewpoint_score" not in writer_prompt
    assert "confidence" not in writer_prompt


async def test_the_critic_gets_the_deterministic_scope_signals(session_factory):
    """overclaim 那一项要有可核对的依据，而不是让 Critic 凭感觉判。

    参考信号只给 Critic（Writer 不需要按范围信号调整写法），并且**只作参考
    事实**——它不混进 Critic 自己的分数。
    """
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    prompts = prompts_by_kind(llm)
    rendered = format_scope_signals(out["selected_angle"])
    assert rendered in prompts["critic"]
    assert "证据能支撑的范围" in rendered
    assert rendered not in prompts["candidates"]


# ---------------------------------------------------------------------------
# 正文回指观点
# ---------------------------------------------------------------------------


async def test_every_candidate_points_back_at_the_viewpoint_it_expresses(session_factory):
    """`viewpoint_ref` 由工具赋，不采信模型给的值。

    它是"这条正文是照哪个观点写的"的唯一凭证——让被检查的一方填写检查依据，
    Critic 的 `viewpoint_consistency` 就没有依据可判了。
    """
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    angle = out["selected_angle"]
    assert angle.angle_id == "a1"
    assert len(out["candidates"]) == 3
    assert {c.viewpoint_ref for c in out["candidates"]} == {angle.angle_id}
    # 模型给不出这个值：假 LLM 的 candidates 响应里根本没有这一项
    assert "viewpoint_ref" not in prompts_by_kind(llm)["candidates"]


async def test_the_candidate_keeps_pointing_at_the_same_viewpoint_after_a_revision(
    session_factory,
):
    """改稿是原地替换，回指不能被换掉。

    换掉的话，Critic 会把一条"照判断甲写的"正文当成"照判断乙写的"来判，
    而它刚刚给出的意见正是针对前者的。改过稿的正文只换了措辞，
    它表达的还是同一个判断。
    """
    llm = revision_path()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    revised = [c for c in out["candidates"] if c.revision > 0]
    assert revised, "这条路径必须真的改过稿，否则断言的是空集"
    assert {c.viewpoint_ref for c in revised} == {out["selected_angle"].angle_id}


async def test_the_revision_prompt_carries_the_same_viewpoint_as_the_draft(session_factory):
    """改稿也必须知道"不能改掉的是哪个判断"（工作单第二十六节）。

    `revise.txt` 里少传 `{{VIEWPOINT}}` 不会报错，只会让模型拿到一个改稿
    目标（"把这段改利落点"）却没有改稿的非目标——于是它很自然地把 thesis
    顺手也换了。这条断言的是真实发出去的那段文本。
    """
    llm = revision_path()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    prompts = prompts_by_kind(llm)
    assert "revise" in prompts, "这条路径必须真的走到改稿"
    rendered = format_viewpoint(out["selected_angle"])
    assert rendered in prompts["revise"]
