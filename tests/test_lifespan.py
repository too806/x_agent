"""应用生命周期：checkpointer 在启动时创建一次、关闭时释放，不在请求里创建。"""

import asyncio
import logging

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db.checkpointer import create_checkpointer
from app.loops import selector_loop_factory
from app.main import app, get_runtime
from app.services.x_api import XApiError
from app.services.x_service import XService
from fakes import FakeXApiClient
from support import CHECKPOINT_URL, api_for, runtime_with_checkpointer


def test_uvicorn_loop_factory_works_with_psycopg():
    """服务真实启动路径用的 loop 必须能跑 psycopg 异步驱动。

    uvicorn 0.36+ 在 Windows 上默认选 ProactorEventLoop，psycopg 异步连接会直接
    失败，因此 run.py 显式传入 selector_loop_factory。这里按同样的方式建 loop 并
    真实连一次库，防止该配置被改回去。
    """

    async def select_one() -> int:
        async with await psycopg.AsyncConnection.connect(CHECKPOINT_URL) as conn:
            cur = await conn.execute("select 1")
            return (await cur.fetchone())[0]

    assert asyncio.run(select_one(), loop_factory=selector_loop_factory) == 1


async def test_lifespan_builds_and_tears_down_runtime(monkeypatch):
    monkeypatch.setattr(settings, "langgraph_checkpoint_database_url", CHECKPOINT_URL)

    with pytest.raises(RuntimeError):
        get_runtime()  # 启动前不可用

    async with app.router.lifespan_context(app):
        runtime = get_runtime()
        assert runtime.graph is not None
        assert runtime.session_factory is not None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200

    # 关闭后 runtime 被清空，不能残留半关闭的连接资源
    with pytest.raises(RuntimeError):
        get_runtime()


async def test_checkpointer_releases_its_pool_on_shutdown():
    """关闭时连接池必须真正关闭，不能留下未释放的数据库连接。"""
    async with create_checkpointer(CHECKPOINT_URL) as saver:
        pool = saver.conn
        assert not pool.closed
    assert pool.closed


async def test_checkpointer_is_created_once_per_process(monkeypatch):
    """checkpointer 只在 lifespan 启动时建一次，请求里不会重复创建连接池。"""
    created: list[str] = []
    real_create = create_checkpointer

    def counting(url: str):
        created.append(url)
        return real_create(url)

    monkeypatch.setattr("app.main.create_checkpointer", counting)
    monkeypatch.setattr(settings, "langgraph_checkpoint_database_url", CHECKPOINT_URL)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(3):
                assert (await client.get("/health")).status_code == 200
            # 一次真实读库的请求（Run 不存在 → 404），同样不应触发新的 checkpointer
            assert (
                await client.get("/api/runs/00000000-0000-0000-0000-000000000000")
            ).status_code == 404

    assert created == [CHECKPOINT_URL]  # 只创建了一次


async def test_logs_do_not_leak_secrets(session_factory, monkeypatch, caplog):
    monkeypatch.setattr(settings, "x_api_bearer_token", "super-secret-token")
    x_service = XService(
        FakeXApiClient(error=XApiError("X API returned 401: bearer super-secret-token rejected"))
    )

    with caplog.at_level(logging.DEBUG):
        async with runtime_with_checkpointer(session_factory, x_service=x_service) as runtime:
            async with api_for(runtime) as api:
                resp = await api.post("/api/runs", json={"topic": "AI coding agent"})

    assert resp.status_code == 502
    assert "super-secret-token" not in caplog.text
    assert "***" in caplog.text  # 日志同样经过脱敏
