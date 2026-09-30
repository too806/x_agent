"""Writer / Critic 接入 StyleRecipe 的端到端行为（Phase 5B.2 工作单第二十一～二十三节）。

这一层要证明的是三条**跨模块**的承诺，单测各自的模块都证明不了：

1. **有 Profile 时，recipe 真的进了 state 并到了 prompt 里**——不是
   "函数返回得对"，而是"跑完一轮，Writer 与 Critic 手上拿到的是同一个约束"。
2. **写进数据库的样本原文，一个字都不会出现在 prompt 里**（工作单第四十节）。
   这条只能这样测：库里真的塞一条带独特标记的样本，然后去 prompt 里找那个标记。
   没有样本时测它只是同义反复。
3. **没有 Profile 时，整条链路照常跑完**（Smoke 4 的离线形态）。结构约束是
   增强，不是前置条件——把它做成硬依赖，等于让语料没采够时 Agent 直接罢工。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import StyleProfile, StyleSample, TrackedAccount
from app.repositories import create_agent_run
from app.style.labels import ending_label, hook_label, structure_label
from app.style.schemas import DensityLevel, EndingType, StyleRecipe
from app.style.taxonomy import HookType, StructureType
from fakes import critic_payload, revise_payload
from support import api_for, runtime_with_checkpointer
from test_admin_api import admin_api
from test_agent_loop import HAPPY_PATH, config_for, decision, drive, happy_llm


async def _make_topic_run(session_factory, topic: str = "AI coding agent") -> str:
    """造一个 V1 topic 链路的 Run：有 topic、没有 goal。

    刻意不复用 `test_admin_api._make_run`：那里是**控制台测试**的播种器，
    这个文件是**风格链路**的测试。为了少写三行去跨模块 import 一个下划线
    私有函数，会把两个测试文件的生命周期绑在一起——那边重构一次，这里
    跟着红，而红的原因与风格无关。
    """
    async with session_factory() as session:
        run = await create_agent_run(session, topic)
        return str(run.id)

#: 语料里的一条样本，带一个**只可能来自样本**的独特标记。
#: 断言 prompt 里找不到它就是断言"样本原文没有泄漏"。
SAMPLE_MARKER = "独特标记符ZQX"
SAMPLE_TEXT = (
    f"先把判断放在最前面：{SAMPLE_MARKER} 这件事的瓶颈不在模型，在调度。"
    "多数团队把钱花在了错的地方，所以越优化越慢。"
)

COMPUTED_AT = datetime(2026, 9, 30, 11, 0, tzinfo=timezone.utc)


async def seed_corpus_profile(session_factory, **overrides) -> StyleProfile:
    """往 `style_profile` 里写一条 corpus 级 Profile。"""
    values = dict(
        scope="corpus",
        sample_count=42,
        computed_at=COMPUTED_AT,
        feature_version="1.0",
        taxonomy_version="1.0",
        median_length=210.0,
        p25_length=150.0,
        p75_length=300.0,
        median_paragraphs=2.0,
        mean_sentence_count=4.0,
        emoji_density=0.0,
        question_density=1.0,
        number_density=2.0,
        english_ratio=0.05,
        structure_distribution={StructureType.OPINION_FIRST.value: 1.0},
        hook_distribution={"question": 1.0},
        opening_distribution={"question": 1.0},
        closing_distribution={"statement": 1.0},
        sentence_length_distribution={"p25": 10.0, "p50": 18.0, "p75": 30.0},
    )
    values.update(overrides)
    async with session_factory() as session:
        row = StyleProfile(**values)
        session.add(row)
        await session.commit()
        return row


async def seed_a_sample_row(session_factory) -> None:
    """往 corpus 里放一条真实样本（含独特标记）。"""
    async with session_factory() as session:
        account = TrackedAccount(username="sample_account", category="AI")
        session.add(account)
        await session.flush()
        session.add(
            StyleSample(
                tracked_account_id=account.id,
                platform_post_id="post-1",
                author_username="sample_account",
                content=SAMPLE_TEXT,
                likes=100,
                included=True,
                structure_type=StructureType.OPINION_FIRST.value,
                hook_type="question",
            )
        )
        await session.commit()


def prompts_by_kind(llm) -> dict[str, str]:
    """假 LLM 记录下来的 prompt，按步骤归类。"""
    return {call["kind"]: call["prompt"] for call in llm.calls}


# ---------------------------------------------------------------------------
# 有 Profile：recipe 进 state，并到达 Writer 与 Critic
# ---------------------------------------------------------------------------


async def test_recipe_from_the_corpus_profile_reaches_the_writer(session_factory):
    await seed_corpus_profile(session_factory)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

        recipe = out["style_recipe"]
        assert isinstance(recipe, StyleRecipe)
        # 结构来自语料里最常见的那一类，长度从中位数收敛而来
        assert recipe.structure_type is StructureType.OPINION_FIRST
        # §六 的三段长度语义在**生产路径**上也要成立。这一支用的是测试
        # persona 的上限（280 > 语料中位数 210），所以上限**不该**起作用：
        # 三个数里有两个同值，但第三个（硬上限）必须照实记着，
        # 而不是因为"这次没用上"就丢掉。
        assert recipe.observed_target_length == 210
        assert recipe.hard_max_length is not None and recipe.hard_max_length > 210
        assert recipe.effective_target_length == 210
        assert recipe.is_length_capped is False
        assert not any("硬上限" in reason for reason in recipe.reasons)
        # 证据包里的事实条数决定证据要求档位
        assert recipe.evidence_requirement is not None
        # 事后能回到那一版 Profile
        assert recipe.profile_ref == f"corpus@{COMPUTED_AT.isoformat()}"
        assert any(COMPUTED_AT.isoformat() in reason or "42 条真实样本" in reason
                   for reason in recipe.reasons)

        # Writer 的 prompt 里印的是**约束**，不是样本
        writer_prompt = prompts_by_kind(llm)["candidates"]
        assert "- 结构：" in writer_prompt
        assert "选择依据：" in writer_prompt
        assert "210 字" in writer_prompt


async def test_a_platform_cap_reaches_the_writer_as_a_cap_not_as_the_corpus_target(
    session_factory,
):
    """语料中位数**高于**平台上限时，整条链路要如实说出这是两件事。

    这一支是**今天真实发生的情形**：语料统计出 155 字，而 `persona.max_length`
    是 140（测试 persona 是 280，所以这里把语料中位数抬到 500 来构造同样的
    关系）。它比上一条重要，因为它才是"语料目标被平台压过"的那条路径，
    而它现在在生产里天天走到。

    要验的还是同一条纪律：Writer 收到的**不是**"目标 280 字"，而是
    "不超过 280 字，因为平台限制，语料里典型长度是 500"——少了后半句，
    模型会以为语料本来就推荐写短，然后为了凑短把事实丢掉。
    """
    await seed_corpus_profile(session_factory, median_length=500.0)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

        recipe = out["style_recipe"]
        assert recipe.observed_target_length == 500
        assert recipe.hard_max_length is not None and recipe.hard_max_length < 500
        assert recipe.effective_target_length == recipe.hard_max_length
        assert recipe.is_length_capped is True
        # 语料目标**没有被改写成**执行值
        assert recipe.observed_target_length != recipe.effective_target_length
        # 而且这件事在依据里说得清清楚楚，不是悄悄取个小值
        assert any("硬上限" in reason for reason in recipe.reasons)

        writer_prompt = prompts_by_kind(llm)["candidates"]
        # 上限印出来了，语料目标也印出来了，并且明说这不是语料的结论。
        assert f"不超过 {recipe.hard_max_length} 字" in writer_prompt
        assert "500 字" in writer_prompt
        assert "不是语料认为该写多短" in writer_prompt


async def test_critic_receives_the_same_recipe_as_the_writer(session_factory):
    """Critic 必须拿**同一个** recipe，否则它按另一个目标去判 style_fit。"""
    await seed_corpus_profile(session_factory)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    prompts = prompts_by_kind(llm)
    recipe = out["style_recipe"]
    from app.agent.prompts import format_style_recipe

    rendered = format_style_recipe(recipe)
    assert rendered in prompts["candidates"]
    assert rendered in prompts["critic"]


async def test_writer_and_critic_get_no_sample_text_even_when_the_corpus_is_populated(
    session_factory,
):
    """**工作单第四十节的机器守卫**：语料原文一个字都不进 prompt。

    库里真的有一条带独特标记的样本，所以这条断言是有内容的——把 recipe 换成
    "检索几条样本塞进 prompt" 的实现，标记会立刻出现在 candidates/critic 的
    prompt 里，测试红。
    """
    await seed_corpus_profile(session_factory)
    await seed_a_sample_row(session_factory)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    prompts = prompts_by_kind(llm)
    for kind in ("candidates", "critic"):
        assert SAMPLE_MARKER not in prompts[kind], f"{kind} 的 prompt 里出现了样本原文"

    # 样本正文也没有混进 state（state 里只有约束）
    serialized_state = repr(out)
    assert SAMPLE_MARKER not in serialized_state
    assert SAMPLE_TEXT not in serialized_state


async def test_the_recipe_stays_stable_across_the_revision_loop(session_factory):
    """改稿要收敛到**同一个**结构，否则改完就漂走了。

    revise 工具从 state 读 recipe，不重新查库——所以这里断言改稿那一步的
    prompt 里也是同一个约束。
    """
    from app.agent.prompts import format_style_recipe

    await seed_corpus_profile(session_factory)
    llm = happy_llm(
        critic=[critic_payload("revise", focus="c1", score=4.0), critic_payload("accept", score=9.0)],
        revise=revise_payload("改好的正文。"),
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

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    rendered = format_style_recipe(out["style_recipe"])
    assert rendered in prompts_by_kind(llm)["revise"]


# ---------------------------------------------------------------------------
# 没有 Profile：照常跑完（Smoke 4）
# ---------------------------------------------------------------------------


async def test_a_run_without_any_profile_still_completes(session_factory):
    """语料没采够时 Agent 必须照常工作——结构约束是增强，不是前置条件。"""
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

        # 仍然走到人工审核
        assert "__interrupt__" in out
        assert out["draft_content"]

        recipe = out["style_recipe"]
        assert isinstance(recipe, StyleRecipe)
        # 中性约束：不指定结构，但**如实说明**为什么
        assert recipe.structure_type is StructureType.UNCLASSIFIED
        assert recipe.observed_target_length is None
        # 而且**不得**拿平台硬上限当作目标长度。"没有语料依据"与"目标就是上限"
        # 是两个不同的陈述，后者会让 Writer 收到一个语料从未支持的长度，
        # 而它在报告里看起来与"语料说该写这么长"一模一样。
        assert recipe.effective_target_length is None
        assert recipe.reasons

        # prompt 里是一段**说明了为什么没有约束**的中性文本，而不是空占位符。
        # 注意走的不是 `format_style_recipe(None)` 那条分支：`resolve_recipe`
        # 从不返回 None，它返回一个中性 recipe，所以这里印的是"未分类 +
        # 未取得 Style Profile"的依据。模型看到一片空白会自己补一套隐含的
        # 结构约束，而那套约束既不在 trace 里也不在 Run Detail 里，事后无法解释。
        writer_prompt = prompts_by_kind(llm)["candidates"]
        assert "{{" not in writer_prompt
        assert "未取得 Style Profile" in writer_prompt
        assert "选择依据：" in writer_prompt


async def test_a_profile_with_no_samples_is_treated_as_absent(session_factory):
    """`sample_count=0` 的 Profile 与"没有 Profile"同义：没有统计量可依据。"""
    await seed_corpus_profile(session_factory, sample_count=0, median_length=None)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    assert out["style_recipe"].structure_type is StructureType.UNCLASSIFIED


async def test_the_latest_profile_wins(session_factory):
    """多条 Profile 时取最新的一版——Run Detail 事后要能回答"当时用的是哪版"。"""
    await seed_corpus_profile(
        session_factory,
        computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        structure_distribution={StructureType.HOW_TO.value: 1.0},
    )
    await seed_corpus_profile(
        session_factory,
        computed_at=datetime(2026, 9, 30, 11, 0, tzinfo=timezone.utc),
        structure_distribution={StructureType.CONTRAST.value: 1.0},
    )
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    assert out["style_recipe"].structure_type is StructureType.CONTRAST


# ---------------------------------------------------------------------------
# recipe 与候选正文的确定性关系
# ---------------------------------------------------------------------------


async def test_delivered_content_is_not_a_copy_of_the_sample(session_factory):
    """交付的是**新写**的正文，不是样本原文。

    这条只能在"库里真的有样本"时才有意义，所以放在这里而不是纯函数单测里。
    """
    await seed_corpus_profile(session_factory)
    await seed_a_sample_row(session_factory)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        _, out = await drive(runtime, session_factory, llm)

    assert out["draft_content"]
    assert out["draft_content"] != SAMPLE_TEXT
    assert SAMPLE_MARKER not in out["draft_content"]


# ---------------------------------------------------------------------------
# 控制台（工作单第三十九节）
# ---------------------------------------------------------------------------


async def test_the_recipe_read_back_from_the_checkpoint_is_still_an_object(session_factory):
    """读回来的 recipe 必须**还是对象**，且字段还是枚举。

    这条钉的是 SERDE 登记。一个未登记的枚举会被拦成普通字符串，然后 pydantic
    在类型化字段上**又把它校验回枚举**——于是最终对象看起来完全正确、所有旧断言
    全绿、只有日志里在响。这份"正确"完全寄生于那一步再校验：字段哪天改成 `Any`，
    同一段代码就会静默拿到字符串，而 `recipe.structure_type is StructureType.X`
    这类比较会**恒为 False**（下游按枚举分支走的地方全部走错，且不报错）。

    所以这里断言的是**类型**，不是值——值对了不说明类型对。
    """
    await seed_corpus_profile(session_factory)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, _ = await drive(runtime, session_factory, llm)
        snapshot = await runtime.graph.aget_state(config_for(run_id))

    recipe = snapshot.values["style_recipe"]
    assert isinstance(recipe, StyleRecipe)
    assert recipe.structure_type is StructureType.OPINION_FIRST
    assert isinstance(recipe.hook_type, HookType)
    assert isinstance(recipe.density, DensityLevel)
    assert isinstance(recipe.ending_type, EndingType)


async def test_run_detail_shows_the_recipe_that_this_run_actually_used(session_factory):
    """最终一环：checkpoint 里的 recipe 能被人从界面上读到。

    到这一步之前，"recipe 进了 state"已经被证明过好几遍。但**能读到**与
    "在 state 里"是两件事：Run Detail 走 `AdminRunDetail` 这个 response_model，
    schema 里没声明的字段会被**静默丢弃**——后端读到了，界面上一片空白，
    而且不报任何错。所以要有一条断言打在 API 响应上。
    """
    await seed_corpus_profile(session_factory)
    llm = happy_llm()

    async with runtime_with_checkpointer(session_factory, llm_service=llm) as runtime:
        run_id, out = await drive(runtime, session_factory, llm)
        async with api_for(runtime) as api:
            detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    style = detail["style"]
    assert style is not None

    # 取值与中文标签都到了。标签**必须等于共享标签表的输出**，不是等于一个
    # 写死的字符串——这份标签同时被 Writer / Critic 的 prompt 使用，界面若
    # 自己抄一份，两边漂了不会有任何报错，只有评审时人对不上号。
    assert style["structure_type"] == StructureType.OPINION_FIRST.value
    assert style["structure_label"] == structure_label(StructureType.OPINION_FIRST)
    assert style["hook_label"] == hook_label(out["style_recipe"].hook_type)
    assert style["ending_label"] == ending_label(out["style_recipe"].ending_type)

    # Run Detail 上必须是**三个**数，且与 state 里那一份逐字段一致。
    # 界面上只印一个长度，读者就无从判断它来自语料还是来自平台。
    assert style["observed_target_length"] == out["style_recipe"].observed_target_length == 210
    assert style["hard_max_length"] == out["style_recipe"].hard_max_length
    assert style["effective_target_length"] == out["style_recipe"].effective_target_length
    assert style["effective_target_length"] == style["observed_target_length"] < style["hard_max_length"]
    assert style["reasons"] == out["style_recipe"].reasons
    # 事后能回到那一版 Profile——没有它，这个块就是一组无法复核的参数。
    assert style["profile_ref"] == f"corpus@{COMPUTED_AT.isoformat()}"

    # 界面块里**没有任何样本字段可以装原文**：真正的保证是数据里就没有，
    # 这条断言只是把它写死成回归测试（万一将来有人往 recipe 上加 content）。
    assert SAMPLE_MARKER not in json.dumps(style, ensure_ascii=False)

    # 正文痕迹是**重算**出来的计数，不是存下来的。
    #
    # 这里量的必须是 `draft_content`：`final_content` 只在人工做出决定之后才有
    # （`nodes/human_review.py` 的 approve / edit 分支），而跑到这里的 Run 正停在
    # 待审队列上。若实现只认 `final_content`，**每一个待审的 Run** 都会显示
    # "没有交付正文"——待审的 Run 恰恰是最需要看这一块的地方。
    assert detail["final_content"] is None  # 还没人审过
    assert style["ai_signals"]["char_count"] == len(out["draft_content"])
    assert style["ai_signals"]["char_count"] > 0


async def test_run_detail_has_no_style_block_for_a_topic_run(session_factory):
    """V1 topic 链路的 Run 不该冒出一个空白的"写作结构"块。

    门控条件是 `goal`——topic 链路根本没有 `{{STYLE}}` 这个占位符，
    画一个"结构：不指定结构"出来，会让人以为 Agent 主动选了中性结构。
    """
    run_id = await _make_topic_run(session_factory)

    async with admin_api(session_factory) as api:
        detail = (await api.get(f"/api/admin/runs/{run_id}")).json()

    assert detail["goal"] is None
    assert detail["style"] is None
