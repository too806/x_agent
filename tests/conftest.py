import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app import models  # noqa: F401  确保模型注册到 Base.metadata
from app.db.base import Base
from support import _ALL_TABLES, ADMIN_SYNC, TEST_URL


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_db():
    # 同步建库 + 建表（仅一次）
    admin = create_engine(ADMIN_SYNC)
    with admin.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname='xagent_test'")
        ).scalar()
        if not exists:
            conn.execute(text("CREATE DATABASE xagent_test"))
    admin.dispose()

    sync_eng = create_engine(TEST_URL)
    Base.metadata.create_all(sync_eng)
    # Phase 3 新增列：表已存在时 create_all 不会补列，显式补齐
    with sync_eng.begin() as conn:
        conn.execute(
            text("ALTER TABLE review ADD COLUMN IF NOT EXISTS original_content TEXT")
        )
        conn.execute(
            text("ALTER TABLE review ADD COLUMN IF NOT EXISTS edited_content TEXT")
        )
        conn.execute(
            text("ALTER TABLE review ADD COLUMN IF NOT EXISTS reject_reason TEXT")
        )
    sync_eng.dispose()
    yield


@pytest_asyncio.fixture(scope="session")
async def test_engine(_prepare_test_db) -> AsyncEngine:
    engine = create_async_engine(TEST_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(test_engine):
    async with test_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_ALL_TABLES} CASCADE"))
    yield async_sessionmaker(test_engine, expire_on_commit=False)
