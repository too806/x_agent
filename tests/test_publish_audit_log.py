"""发布审计日志与日志落盘。

这两条是为"下次事故复盘"补的眼睛，不是业务能力。

2026-10-06 调查一条真实 X 帖子的来源时，能拿到的证据只有数据库时间线：后端
没有请求日志，也没有落盘（`cmd /k` 的滚动缓冲区随进程一起没了）。那次只能靠
`auto_review.acted` 反推"不是自动化发的"，却回答不了"是谁发的"。

这里钉住四件事：

1. `/publish` 那一行**必须**带请求上下文——否则等于没记；
2. 缺 User-Agent / Referer / client 时不能把发布打成 500——审计信息永远
   不该成为故障点；
3. 这一行**不能**出现凭据、Cookie 或正文；
4. 落盘会自己建目录、能轮转，且测试**不会**写进仓库的 logs/。
"""

import logging
import os

import pytest
from fastapi import Request

from app.logging_setup import LOG_FILE_NAME, configure_logging
from app.main import _audit_header, _client_host
from app.services.x_service import XService
from fakes import FakeXApiClient
from support import api_for, runtime_with_checkpointer

POST_ID = "1900000000000000001"


# ---------- 共用脚手架 ----------


class _Client:
    """与 test_publish_api.py 同一套假 X 客户端——**不会**真的发帖。"""

    def __init__(self, **kwargs):
        self.api_client = FakeXApiClient(**kwargs)
        self.x_service = XService(self.api_client)


def _client(**kwargs) -> _Client:
    return _Client(**kwargs)


async def _start_run(api, topic: str = "AI coding agent") -> dict:
    resp = await api.post("/api/runs", json={"topic": topic})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "waiting_for_review"
    return resp.json()


async def _prepared(api) -> dict:
    started = await _start_run(api)
    resp = await api.post(f"/api/runs/{started['run_id']}/review", json={"action": "approve"})
    assert resp.status_code == 200, resp.text
    return started


def _publish_line(caplog) -> str:
    """把那条审计日志渲染成最终写进文件的字符串。"""
    for record in caplog.records:
        if record.name == "app.main" and record.getMessage().startswith("publish requested"):
            return record.getMessage()
    raise AssertionError("没有发出 'publish requested' 日志行")


def _request(headers: dict[str, str], client=None) -> Request:
    """按 ASGI 原始 scope 造一个 Request。

    这是唯一能**确定**构造出"头完全不存在 / client 为 None"的办法——
    走 httpx 的话它会自己补一个默认 User-Agent，测不出缺头那条路。
    """
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/runs/some-run/publish",
        "query_string": b"",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }
    if client is not None:
        scope["client"] = client
    return Request(scope)


@pytest.fixture
def _restore_root_handlers():
    """用完把测试挂上的 handler 摘掉并关闭。

    必须关：Windows 上打开中的文件删不掉，pytest 清理 tmp_path 会失败；而且
    它会一直挂在 root 上，把后面所有测试的日志都写进一个已被删掉的临时文件。
    """
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


# ---------- 1. 审计日志带请求上下文 ----------


async def test_publish_log_carries_request_context(session_factory, caplog):
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            with caplog.at_level(logging.INFO, logger="app.main"):
                resp = await api.post(
                    f"/api/runs/{started['run_id']}/publish",
                    headers={
                        "User-Agent": "AuditProbe/1.0",
                        "Referer": "http://127.0.0.1:5173/review",
                    },
                )
            assert resp.status_code == 200, resp.text

    line = _publish_line(caplog)
    assert f"run_id={started['run_id']}" in line
    assert "user_agent=AuditProbe/1.0" in line
    assert "referer=http://127.0.0.1:5173/review" in line
    assert "client_host=" in line
    assert f"pid={os.getpid()}" in line


# ---------- 2. 缺 UA / Referer / client 不能出事 ----------


@pytest.mark.parametrize(
    "client, expected",
    [(None, "-"), (("127.0.0.1", 5555), "127.0.0.1:5555")],
    ids=["no-client", "with-client"],
)
def test_client_host_survives_missing_client(client, expected):
    assert _client_host(_request({}, client)) == expected


@pytest.mark.parametrize("name", ["user-agent", "referer"])
def test_audit_header_missing_becomes_dash(name):
    assert _audit_header(_request({}), name) == "-"


def test_audit_header_collapses_newlines():
    """请求头是客户端可控的：塞进换行就能伪造出额外的日志行。"""
    request = _request({"user-agent": "evil\r\npublish requested run_id=fake"})
    value = _audit_header(request, "user-agent")
    assert "\n" not in value and "\r" not in value
    assert value == "evil publish requested run_id=fake"


async def test_publish_succeeds_without_user_agent_or_referer(session_factory, caplog):
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _prepared(api)
            with caplog.at_level(logging.INFO, logger="app.main"):
                resp = await api.post(
                    f"/api/runs/{started['run_id']}/publish",
                    headers={"User-Agent": "", "Referer": ""},
                )
            # 审计信息缺席不该让一次合法发布失败。
            assert resp.status_code == 200, resp.text
            assert resp.json()["outcome"] == "published"

    assert "user_agent=" in _publish_line(caplog)


# ---------- 3. 这一行不泄漏凭据 / Cookie / 正文 ----------


async def test_publish_log_does_not_leak_secrets_or_content(session_factory, caplog):
    x = _client(post_id=POST_ID)
    async with runtime_with_checkpointer(session_factory, x_service=x.x_service) as runtime:
        async with api_for(runtime) as api:
            started = await _start_run(api)
            draft_content = started["draft_content"]
            resp = await api.post(f"/api/runs/{started['run_id']}/review", json={"action": "approve"})
            assert resp.status_code == 200, resp.text

            with caplog.at_level(logging.DEBUG):
                resp = await api.post(
                    f"/api/runs/{started['run_id']}/publish",
                    headers={
                        # 故意把凭据塞进请求头：日志里出现任何一个都算泄漏。
                        "Authorization": "Bearer super-secret-token",
                        "Cookie": "session=super-secret-cookie",
                        "User-Agent": "AuditProbe/1.0",
                    },
                )
            assert resp.status_code == 200, resp.text

    line = _publish_line(caplog)
    for forbidden in ("Authorization", "Cookie", "Bearer", "super-secret-token",
                      "super-secret-cookie"):
        assert forbidden not in line, f"{forbidden} 出现在发布审计日志里"

    # 更严的一层：整段捕获里也不该有凭据或正文。
    text = caplog.text
    assert "super-secret-token" not in text
    assert "super-secret-cookie" not in text
    assert draft_content not in text


# ---------- 4 / 5. 落盘与轮转（用临时目录，不碰仓库的 logs/） ----------


def test_configure_logging_creates_dir_and_writes(tmp_path, _restore_root_handlers):
    target = tmp_path / "nested" / "logs"
    assert not target.exists()

    path = configure_logging(log_dir=target, to_file=True)

    assert path == target / LOG_FILE_NAME
    assert target.is_dir()  # 目录不存在时自动建
    logging.getLogger("app.audit_probe").info("hello from tmp log")
    assert path.exists()  # delay=True：写第一条才真正建文件
    assert "hello from tmp log" in path.read_text(encoding="utf-8")


def test_configure_logging_is_idempotent(tmp_path, _restore_root_handlers):
    """重复调用不能挂第二个 handler——否则每条日志会被写两遍。"""
    root = logging.getLogger()
    configure_logging(log_dir=tmp_path, to_file=True)
    after_first = len(root.handlers)
    configure_logging(log_dir=tmp_path, to_file=True)
    assert len(root.handlers) == after_first


def test_rotating_handler_rolls_over(tmp_path, _restore_root_handlers):
    path = configure_logging(log_dir=tmp_path, to_file=True, max_bytes=512, backup_count=2)

    probe = logging.getLogger("app.rotation_probe")
    for i in range(50):
        probe.info("%s %d", "x" * 120, i)

    assert path.exists()
    assert (tmp_path / f"{LOG_FILE_NAME}.1").exists()
    # backupCount 是上限：不该无限增长。
    assert not (tmp_path / f"{LOG_FILE_NAME}.3").exists()
    total = sum(p.stat().st_size for p in tmp_path.glob(f"{LOG_FILE_NAME}*"))
    assert total < 50 * 140  # 确实被轮转削过，不是全堆在一个文件里


def test_configure_logging_without_file_returns_none(tmp_path, _restore_root_handlers):
    """默认（`to_file=False`）不落盘——`app.main` 被 pytest import 时的行为。"""
    assert configure_logging(log_dir=tmp_path) is None
    assert not (tmp_path / LOG_FILE_NAME).exists()
