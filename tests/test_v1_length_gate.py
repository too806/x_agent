"""V1（topic）链路的长度交付约束。

Agent 链路（goal）有三处长度执行点：Critic 的确定性拦截、finalize 的选稿、
发布预检。V1 只有最后一处——生成阶段不设防，于是"审核队列里躺着一条谁都没打算
发的超长稿"要一路走到发布预检才被拦下，而人工审核那一步已经先把一条发不出去的
正文收下了。

这一轮把 V1 的生成阶段补上：判界读的必须是**同一个尺子**
（`app/agent/post_structure.over_hard_limit`）与**运行时真正生效的那个上限**
（`persona.max_length`）。这组用例钉的就是这条约束本身——

- 不超上限：draft 照建，原样进人工审核；
- 超上限：**不建 draft、不进审核**，Run 以失败收场，错误信息写明确切字数；
- 边界是 `>`：140 合规、141 超限；
- 上限来自运行时配置：换一个数，判界就跟着换；
- 不截断、不改写：合规的正文一字不动地落库。

`tests/support.py` 里的 `PERSONA.max_length = 280` 是刻意的单元测试配置（见那里的
说明），所以"生产真的按 140 在拦"这件事必须用**生产的那份 persona**单独证明，
不能靠 fixture 顺带成立——本文件末尾的几条就是干这个的。
"""

from pathlib import Path

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

import app.main as main
from app.agent.nodes.finalize import AgentOverlongError, make_finalize_node
from app.agent.nodes.generate import make_generate_node
from app.agent.persona import Persona, load_persona
from app.agent.schemas import Candidate, CriticResult
from app.config import settings
from app.models import AgentRun, Draft, Review
from app.repositories import create_agent_run
from fakes import FakeLLMService, draft_json
from support import PERSONA, api_for, runtime_with_checkpointer

TOPIC = "AI coding agent"

#: 生产入口读到的那份 persona（`config/persona.yaml`）。V1 的判界必须按它成立，
#: 而不是按 `support.PERSONA`（280）——后者是给别的服务行为测试用的长正文配置。
PRODUCTION_PERSONA = load_persona()


async def _start_run(session_factory, topic: str = TOPIC) -> str:
    async with session_factory() as session:
        run = await create_agent_run(session, topic)
        return str(run.id)


async def _drafts(session_factory) -> list[Draft]:
    async with session_factory() as session:
        return list((await session.execute(select(Draft))).scalars().all())


async def _reviews(session_factory) -> list[Review]:
    async with session_factory() as session:
        return list((await session.execute(select(Review))).scalars().all())


async def _runs(session_factory) -> list[AgentRun]:
    async with session_factory() as session:
        return list((await session.execute(select(AgentRun))).scalars().all())


async def _body(content: str, *, persona=PRODUCTION_PERSONA, session_factory):
    """跑一次 V1 的生成节点，返回它写给下游的那份 state 补丁。"""
    node = make_generate_node(FakeLLMService(text=draft_json(content=content)), persona, session_factory)
    run_id = await _start_run(session_factory)
    return await node({"run_id": run_id, "topic": TOPIC, "research_results": []})


def _text_of(excinfo) -> str:
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# 1 / 2：合规进审核，超长不落 draft 也不进审核（走真实 HTTP 入口）
# ---------------------------------------------------------------------------


async def _post_topic_run(session_factory, **runtime_kwargs):
    async with runtime_with_checkpointer(session_factory, **runtime_kwargs) as runtime:
        async with api_for(runtime) as api:
            return await api.post("/api/runs", json={"topic": TOPIC})


async def test_v1_within_the_limit_creates_the_draft_and_reaches_review(session_factory):
    """不超过上限：原流程一点没变——建 draft、过 guardrail、停在人工审核。"""
    body = "字" * PRODUCTION_PERSONA.max_length
    resp = await _post_topic_run(
        session_factory,
        llm_service=FakeLLMService(text=draft_json(content=body)),
        persona=PRODUCTION_PERSONA,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "waiting_for_review"
    assert data["draft_id"]
    assert data["error"] is None

    # 交付给审核者的正文与模型产出逐字相同——生成阶段没有动过它
    assert data["draft_content"] == body
    assert data["review"]["type"] == "content_review"
    assert data["review"]["content"] == body

    drafts = await _drafts(session_factory)
    assert [d.content for d in drafts] == [body]
    assert drafts[0].version == 1

    run = (await _runs(session_factory))[0]
    assert run.status == "waiting_for_review"
    assert run.finished_at is None


async def test_v1_over_the_limit_writes_no_draft_and_asks_no_human(session_factory):
    """超过上限：不建 draft、不进人工审核，Run 如实记为失败。

    "draft 存在"是下游（审核队列、发布预检、控制台）判断"这次 Run 有没有东西可以
    交付"的唯一信号。所以这条路径的关键断言是**没有 draft**——写一条进去，等于
    让这次 Run 看起来产出了可交付的内容。
    """
    body = "字" * (PRODUCTION_PERSONA.max_length + 1)
    resp = await _post_topic_run(
        session_factory,
        llm_service=FakeLLMService(text=draft_json(content=body)),
        persona=PRODUCTION_PERSONA,
    )

    assert resp.status_code == 500
    data = resp.json()
    assert data["status"] == "failed"
    assert data["draft_id"] is None
    assert data["review"] is None  # 没有人工审核这一步
    # 错误信息说得清是哪种超长、超了多少：审核者/排障的人不用自己数
    assert "超过硬上限 140 字 1 字" in data["error"]

    assert await _drafts(session_factory) == []
    assert await _reviews(session_factory) == []

    run = (await _runs(session_factory))[0]
    assert run.status == "failed"
    assert run.finished_at is not None
    assert "超过硬上限" in run.error


# ---------------------------------------------------------------------------
# 3：超长正文没有别的状态转换可以绕过
# ---------------------------------------------------------------------------


async def test_v1_overlong_has_no_other_transition_into_review(session_factory):
    """判界发生在 graph 内部，所以这条 Run 根本走不到 guardrail / human_review。

    先证明只走到 `generate`（`snapshot.next` 还是它自己），再证明 resume 也没有
    第二条路——第二次仍然抛同一个错、仍然一条 draft 都不写。如果哪天有人把判界
    挪到了 human_review 之后，或者给它加一条"绕过"的分支，这里会红。
    """
    body = "字" * (PRODUCTION_PERSONA.max_length + 5)
    llm = FakeLLMService(text=draft_json(content=body))

    async with runtime_with_checkpointer(
        session_factory, llm_service=llm, persona=PRODUCTION_PERSONA
    ) as runtime:
        run_id = await _start_run(session_factory)
        config = {"configurable": {"thread_id": run_id}}

        with pytest.raises(AgentOverlongError):
            await runtime.graph.ainvoke({"run_id": run_id, "topic": TOPIC}, config=config)

        snapshot = await runtime.graph.aget_state(config)
        # 下一步仍然是 generate——research 之后的 checkpoint 上
        # guardrail / human_review 都还没轮到，而它们也拿不到任何正文
        assert snapshot.next == ("generate",)
        assert snapshot.values.get("draft_id") is None
        assert snapshot.values.get("draft_content") is None

        # resume 也没有别的出口：同一个错，同一份空产出
        with pytest.raises(AgentOverlongError):
            await runtime.graph.ainvoke(None, config=config)

    assert await _drafts(session_factory) == []
    assert await _reviews(session_factory) == []


# ---------------------------------------------------------------------------
# 4：边界是 `>`，不是 `>=`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length, allowed", [(140, True), (141, False)])
async def test_v1_boundary_is_exactly_the_hard_limit(session_factory, length, allowed):
    """140 合规、141 超限——与 finalize / 发布预检用的是同一个判界口径。"""
    body = "字" * length

    if allowed:
        out = await _body(body, session_factory=session_factory)
        assert out["draft_content"] == body
        assert [d.content for d in await _drafts(session_factory)] == [body]
    else:
        with pytest.raises(AgentOverlongError) as excinfo:
            await _body(body, session_factory=session_factory)
        assert "超过硬上限 140 字 1 字" in _text_of(excinfo)
        assert await _drafts(session_factory) == []


# ---------------------------------------------------------------------------
# 5：上限来自运行时配置，不是另抄一个 140
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length, allowed", [(25, True), (26, False)])
async def test_v1_reads_the_limit_from_the_runtime_persona(session_factory, length, allowed):
    """换一个上限，判界就跟着换。

    这条 persona 的上限是 25，不是 140——如果哪一层又写死了一个 140，25 字的正文
    会被拒（而不是被放行），失败信息里也会出现 140 这个没人在配置里写过的数。
    """
    persona = Persona(
        name="tight",
        description="极简",
        tone="conversational",
        style=["简洁"],
        max_length=25,
    )
    body = "字" * length
    llm = FakeLLMService(text=draft_json(content=body))
    node = make_generate_node(llm, persona, session_factory)
    run_id = await _start_run(session_factory)

    if allowed:
        out = await node({"run_id": run_id, "topic": TOPIC, "research_results": []})
        assert out["draft_content"] == body
        assert [d.content for d in await _drafts(session_factory)] == [body]
        assert str(persona.max_length) in llm.calls[0]["prompt"]
        assert "140" not in llm.calls[0]["prompt"]
    else:
        with pytest.raises(AgentOverlongError) as excinfo:
            await node({"run_id": run_id, "topic": TOPIC, "research_results": []})
        assert "超过硬上限 25 字 1 字" in _text_of(excinfo)
        assert await _drafts(session_factory) == []


# ---------------------------------------------------------------------------
# 6：Agent（goal）链路的长度行为没有被改动
# ---------------------------------------------------------------------------


async def test_the_goal_path_keeps_its_own_length_enforcement(session_factory):
    """补齐 V1 不等于把两道闸门合成一道：goal 链路照旧在 finalize 处选稿/失败。

    两件事要在同一条用例里同时成立，否则"统一交付约束"就变成了"把 Agent 那条
    链路的行为改掉"：
    1. 池子里有一条合规的，就绝不选超长的那条（哪怕它分更高）；
    2. 全池超长时抛的是**同一个** `AgentOverlongError`——对下游而言两条链路是
       同一件事："这次 Run 没有可交付的正文"。
    """
    overlong = "字" * (PRODUCTION_PERSONA.max_length + 3)
    short = "一条合规的正文。"
    node = make_finalize_node(session_factory, PRODUCTION_PERSONA)

    await node(
        {
            "run_id": await _start_run(session_factory),
            "goal": "写一条关于 AI coding agent 的帖子",
            "selected_topic": "题目",
            "candidates": [
                Candidate(candidate_id="c-long", label="直接观点", content=overlong),
                Candidate(candidate_id="c-short", label="冲突对比", content=short),
            ],
            "critic_results": [
                CriticResult(candidate_id="c-long", decision="accept", score=9.5),
                CriticResult(candidate_id="c-short", decision="accept", score=6.0),
            ],
            "step_count": 3,
        }
    )
    assert [d.content for d in await _drafts(session_factory)] == [short]

    with pytest.raises(AgentOverlongError):
        await node(
            {
                "run_id": await _start_run(session_factory),
                "goal": "写一条关于 AI coding agent 的帖子",
                "selected_topic": "题目",
                "candidates": [
                    Candidate(candidate_id="c1", label="直接观点", content=overlong),
                ],
                "critic_results": [CriticResult(candidate_id="c1", decision="accept", score=9.0)],
                "step_count": 3,
            }
        )
    assert [d.content for d in await _drafts(session_factory)] == [short]


# ---------------------------------------------------------------------------
# 9：不截断、不改写
# ---------------------------------------------------------------------------


async def test_v1_never_truncates_the_generated_body(session_factory):
    """合规的正文一字不动地落库：不砍末尾、不补字、不重排。

    截断凑合规比超长更糟——句子残缺、观点失真、事实关系被破坏，而下游看到的
    是一条"合格"的稿子，没人会再去核对它原本要说什么。
    """
    body = "第一句钩子。" + "正文句子。" * 5  # 长度由夹具自己决定，不依赖 140
    out = await _body(body, session_factory=session_factory)

    assert out["draft_content"] == body
    assert len(out["draft_content"]) == len(body)
    assert [d.content for d in await _drafts(session_factory)] == [body]


# ---------------------------------------------------------------------------
# 生产配置：140（与 tests/support.py 的 280 是两回事）
# ---------------------------------------------------------------------------


def test_the_production_limit_is_140_and_reads_from_the_config_file():
    """生产上限是 140，而且它确实是**从配置文件读出来的**那个值。

    只断言 `load_persona().max_length == 140` 的话，"加载器里写死了 140、
    配置文件早被改掉"这种情况是看不出来的——所以直接读同一份 YAML 对照。
    """
    assert PRODUCTION_PERSONA.max_length == 140
    raw = yaml.safe_load(Path(settings.persona_path).read_text(encoding="utf-8"))
    assert raw["max_length"] == 140
    assert len(PRODUCTION_PERSONA.name) > 0


def test_the_production_entry_wires_the_real_limit_into_the_chain():
    """`build_runtime`（生产入口）把同一个 persona 交给两条链路，两侧上限一致。

    生成阶段的判界读 Graph 依赖里的 persona，发布预检读 PublishService 构造时传的
    `content_max_length`。这两个数必须是 config/persona.yaml 里的那一个——只要有一
    侧走了默认值或另一个数，就会变成"生成说合格、发布却拦下"。
    """
    runtime = main.build_runtime(InMemorySaver())

    assert runtime.persona.max_length == 140
    assert runtime.publish_service._content_max_length == 140
    assert runtime.persona is not PRODUCTION_PERSONA  # 各自从配置读，不共享实例


async def test_the_280_fixture_does_not_mask_the_production_limit(session_factory):
    """同一条 200 字的正文：280 的夹具放行，生产的 140 拦下。

    这条同时说明两件事：
    1. `tests/support.py` 的 280 是**能真正生效**的配置（判界读的是构造时那份
       persona，不是写死的 140）——那正是它作为单元测试夹具的用途；
    2. 但它不会把生产约束掩盖掉：同一份正文在生产 persona 下被拒，而且拒的理由
       里写的正是 140。少了这一条，"280 放行"就会被误读成"V1 不拦长度"。
    """
    assert PERSONA.max_length == 280 and PRODUCTION_PERSONA.max_length == 140
    body = "字" * 200

    out = await _body(body, persona=PERSONA, session_factory=session_factory)
    assert out["draft_content"] == body

    with pytest.raises(AgentOverlongError) as excinfo:
        await _body(body, persona=PRODUCTION_PERSONA, session_factory=session_factory)
    assert "超过硬上限 140 字 60 字" in _text_of(excinfo)
