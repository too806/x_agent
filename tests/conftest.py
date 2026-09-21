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
    # 表已存在时 create_all 既不会补列、也不会补约束（测试库是跨进程复用的，
    # 只有第一次建库才会走 create_all 的完整定义）。必须显式补齐，否则：
    #   - 缺 status/last_error → 相关测试直接报 UndefinedColumn
    #   - 缺 UNIQUE(agent_run_id) → **并发测试会恒真通过**，比报错更糟
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
        conn.execute(
            text("ALTER TABLE published_post ADD COLUMN IF NOT EXISTS status VARCHAR(32)")
        )
        conn.execute(
            text("ALTER TABLE published_post ADD COLUMN IF NOT EXISTS last_error TEXT")
        )
        conn.execute(
            text(
                "ALTER TABLE published_post ADD COLUMN IF NOT EXISTS "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            )
        )
        # 上一次异常退出（或专门做过"去掉约束"的变异验证）可能留下同一
        # agent_run_id 的多行，会让下面的 ADD CONSTRAINT 直接失败、整个 session
        # 起不来。测试库本就是一次性的（每个 test 前都会 TRUNCATE），先清干净。
        conn.execute(text("TRUNCATE published_post"))
        # ADD CONSTRAINT 没有 IF NOT EXISTS，用 pg_constraint 先探一下。
        # 这条是整个发布幂等的**关键路径**：并发首次发布时 INSERT 分支全靠它
        # 才只有一个赢家（去掉后 test_concurrent_claims_exactly_one_wins 会拿到
        # [True, True, True]，三个请求都会去调 X）。
        conn.execute(
            text(
                """
                DO $$ BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_published_post_agent_run_id'
                  ) THEN
                    ALTER TABLE published_post
                      ADD CONSTRAINT uq_published_post_agent_run_id UNIQUE (agent_run_id);
                  END IF;
                END $$;
                """
            )
        )
        # 补列后旧行 status 会是 NULL，把 NOT NULL 也补上（新库由 create_all 保证）
        conn.execute(text("UPDATE published_post SET status = 'published' WHERE status IS NULL"))
        conn.execute(text("ALTER TABLE published_post ALTER COLUMN status SET NOT NULL"))
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
