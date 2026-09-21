import uuid

from sqlalchemy import select

from app.agent.nodes.guardrail import FORBIDDEN_TERMS, evaluate, make_guardrail_node
from app.models import AgentRun, Draft, PublishedPost
from app.models.published_post import STATUS_FAILED, STATUS_PUBLISHED, STATUS_PUBLISHING
from app.repositories import create_agent_run, create_draft
from support import PERSONA

VALID = "AI coding agents ship faster."


def test_valid_content_passes():
    passed, reasons = evaluate(VALID, max_length=280)

    assert passed is True
    assert reasons == []


def test_content_at_max_length_passes():
    passed, reasons = evaluate("a" * 280, max_length=280)

    assert passed is True
    assert reasons == []


def test_empty_content_fails():
    for empty in (None, "", "   ", "\n\t "):
        passed, reasons = evaluate(empty, max_length=280)

        assert passed is False, f"{empty!r} 应判定为空内容"
        assert reasons == ["empty_content"]


def test_too_long_content_fails():
    passed, reasons = evaluate("a" * 281, max_length=280)

    assert passed is False
    assert reasons == ["too_long"]


def test_duplicate_of_published_content_fails():
    passed, reasons = evaluate(VALID, max_length=280, published_contents=[VALID])

    assert passed is False
    assert reasons == ["duplicate_content"]


def test_forbidden_content_fails():
    passed, reasons = evaluate(f"hello, {FORBIDDEN_TERMS[0]} today", max_length=280)

    assert passed is False
    assert reasons == ["forbidden_content"]


def test_forbidden_check_is_case_insensitive():
    passed, reasons = evaluate(FORBIDDEN_TERMS[0].upper(), max_length=280)

    assert passed is False
    assert "forbidden_content" in reasons


def test_basic_format_anomaly_fails():
    # 没有字母/数字
    passed, reasons = evaluate("!!! ??? ---", max_length=280)
    assert passed is False
    assert reasons == ["invalid_format"]

    # 含控制字符
    passed, reasons = evaluate("clean text\x00here", max_length=280)
    assert passed is False
    assert reasons == ["invalid_format"]


def test_newlines_are_allowed():
    passed, reasons = evaluate("line one\nline two", max_length=280)

    assert passed is True
    assert reasons == []


def test_multiple_violations_are_all_collected():
    passed, reasons = evaluate("x" * 300, max_length=280, published_contents=["x" * 300])

    assert passed is False
    assert reasons == ["too_long", "duplicate_content"]


async def test_guardrail_node_reads_published_content_from_database(session_factory):
    """重复判定对比的是数据库里的已发布正文，而不是传入参数。"""
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)
        draft_id = await create_draft(
            session, run_id, VALID, version=1, prompt_version="1", model_name="fake"
        )
    async with session_factory() as session:
        session.add(
            PublishedPost(
                draft_id=uuid.UUID(draft_id),
                agent_run_id=uuid.UUID(run_id),
                body=VALID,
                status=STATUS_PUBLISHED,
                x_post_id="1900000000000000001",
            )
        )
        await session.commit()

    node = make_guardrail_node(PERSONA, session_factory)
    out = await node({"run_id": run_id, "topic": "AI coding agent", "draft_content": VALID})

    assert out["guardrail_passed"] is False
    assert out["guardrail_reasons"] == ["duplicate_content"]


async def test_guardrail_ignores_attempts_that_never_went_out(session_factory):
    """publishing / failed 的尝试记录从未出现在 X 上，不能算作"已发布"去判重。

    Phase 4 之前 published_post 里只有"确实发过"的行，所以按 body 全表扫描是对的。
    现在同一张表还要承载失败与结果未知的尝试，必须靠 x_post_id 过滤。
    """
    for attempt_status in (STATUS_PUBLISHING, STATUS_FAILED):
        async with session_factory() as session:
            run = await create_agent_run(session, "AI coding agent")
            run_id = str(run.id)
            draft_id = await create_draft(
                session, run_id, VALID, version=1, prompt_version="1", model_name="fake"
            )
        async with session_factory() as session:
            session.add(
                PublishedPost(
                    draft_id=uuid.UUID(draft_id),
                    agent_run_id=uuid.UUID(run_id),
                    body=VALID,
                    status=attempt_status,
                )
            )
            await session.commit()

    node = make_guardrail_node(PERSONA, session_factory)
    out = await node({"run_id": run_id, "topic": "AI coding agent", "draft_content": VALID})

    assert out["guardrail_passed"] is True
    assert out["guardrail_reasons"] == []


async def test_guardrail_node_passes_when_nothing_published(session_factory):
    async with session_factory() as session:
        run = await create_agent_run(session, "AI coding agent")
        run_id = str(run.id)

    node = make_guardrail_node(PERSONA, session_factory)
    out = await node({"run_id": run_id, "topic": "AI coding agent", "draft_content": VALID})

    assert out["guardrail_passed"] is True
    assert out["guardrail_reasons"] == []

    # Guardrail 只判定，不写库
    async with session_factory() as session:
        assert (await session.execute(select(AgentRun))).scalars().all()[0].status == "running"
        assert (await session.execute(select(Draft))).scalars().all() == []
