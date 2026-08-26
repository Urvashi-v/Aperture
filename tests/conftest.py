"""Shared test fixtures.

The suite runs against a real PostgreSQL, not SQLite and not a mock. Two of
the planted pathologies are properties of the PostgreSQL planner, and the whole
point of the project is that the analysis is grounded in real database
behaviour; a test suite that swapped in a different engine would be testing
something other than the system.

A dedicated `shop_test` database is created and dropped by the session fixture,
so running the tests never touches the development dataset.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SHOP = REPO_ROOT / "sample-shop"

TEST_DB_NAME = os.environ.get("TEST_POSTGRES_DB", "shop_test")


def _base_settings_from_env() -> tuple[str, str]:
    """Return (admin_dsn, test_dsn) for the configured PostgreSQL instance.

    Imported lazily and before any `shop` module, so that DATABASE_URL can be
    redirected at the test database first.
    """
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5433")
    user = os.environ.get("POSTGRES_USER", "shop")
    password = os.environ.get("POSTGRES_PASSWORD", "shop_local_dev_password")

    admin = f"postgresql://{user}:{password}@{host}:{port}/postgres"
    test = f"postgresql://{user}:{password}@{host}:{port}/{TEST_DB_NAME}"
    return admin, test


# Redirect the application at the test database before `shop.config` is ever
# imported. Settings are cached, so this has to happen at import time.
_ADMIN_DSN, _TEST_DSN = _base_settings_from_env()
os.environ["DATABASE_URL"] = _TEST_DSN.replace(
    "postgresql://", "postgresql+asyncpg://", 1
)
# Keep the pool small in tests: it makes connection leaks show up as a hang in
# a single test rather than as mysterious slowness at the end of the run.
os.environ.setdefault("DB_POOL_SIZE", "5")
os.environ.setdefault("DB_MAX_OVERFLOW", "2")
os.environ.setdefault("SHOP_LOG_FORMAT", "console")
os.environ.setdefault("SHOP_LOG_LEVEL", "WARNING")


async def _recreate_database() -> None:
    import asyncpg

    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        # Terminate leftovers from an interrupted run, otherwise DROP blocks.
        await conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            TEST_DB_NAME,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
        await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    """Skip the whole DB-backed suite with a useful message if PG is not up."""
    import asyncpg

    async def _probe() -> None:
        conn = await asyncpg.connect(_ADMIN_DSN)
        await conn.close()

    try:
        asyncio.run(_probe())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(
            f"PostgreSQL is not reachable at {_ADMIN_DSN.rsplit('@', 1)[-1]} "
            f"({exc}). Start it with: docker compose up -d postgres",
            allow_module_level=True,
        )
    return True


@pytest.fixture(scope="session")
def migrated_database(postgres_available: bool) -> Iterator[str]:
    """Create `shop_test`, migrate it to head, and drop it afterwards.

    Synchronous on purpose: Alembic's env.py calls `asyncio.run`, which cannot
    be nested inside an already-running event loop.
    """
    from alembic import command
    from alembic.config import Config

    asyncio.run(_recreate_database())

    cfg = Config(str(SAMPLE_SHOP / "alembic.ini"))
    cfg.set_main_option("script_location", str(SAMPLE_SHOP / "migrations"))
    command.upgrade(cfg, "head")

    yield _TEST_DSN

    asyncio.run(_drop_database())


async def _drop_database() -> None:
    import asyncpg

    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        await conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            TEST_DB_NAME,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def seeded_database(migrated_database: str) -> str:
    """Load the `tiny` profile once for the whole session.

    Tiny is deliberately too small to reproduce the planner behaviour behind
    P3/P4 - that needs the medium profile and is measured in the evaluation
    harness, not asserted here. What tiny is good for is proving the endpoints
    return correct data and issue the query *shapes* we expect.
    """
    from shop.seed.profiles import get_profile
    from shop.seed.seeder import seed_database

    asyncio.run(seed_database(migrated_database, get_profile("tiny"), random_seed=42))
    return migrated_database


@pytest.fixture(scope="session")
async def app(seeded_database: str):  # noqa: ANN201 - FastAPI app
    from shop.db import dispose_engine, init_engine
    from shop.main import app as fastapi_app

    init_engine()
    yield fastapi_app
    await dispose_engine()


@pytest.fixture(scope="session")
async def client(app) -> AsyncIterator:  # noqa: ANN001, ANN201
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://sample-shop.test"
    ) as async_client:
        yield async_client


@pytest.fixture(scope="session")
async def seed_ids(seeded_database: str) -> dict[str, int]:
    """Real ids from the seeded dataset, so tests never hard-code magic numbers."""
    import asyncpg

    conn = await asyncpg.connect(seeded_database)
    try:
        ids = {
            "user_id": await conn.fetchval("SELECT min(id) FROM users"),
            "seller_id": await conn.fetchval(
                "SELECT min(id) FROM users WHERE is_seller"
            ),
            "product_id": await conn.fetchval("SELECT min(id) FROM products"),
            "order_id": await conn.fetchval("SELECT min(id) FROM orders"),
            "post_id": await conn.fetchval(
                "SELECT min(id) FROM posts WHERE is_published"
            ),
            "review_id": await conn.fetchval("SELECT min(id) FROM reviews"),
            "category_id": await conn.fetchval("SELECT min(id) FROM categories"),
        }
        # A user who actually follows people and has placed orders, so the feed
        # and order-history tests exercise a non-empty path.
        ids["follower_id"] = await conn.fetchval(
            """
            SELECT follower_id FROM follows
            GROUP BY follower_id ORDER BY count(*) DESC LIMIT 1
            """
        )
        ids["buyer_id"] = await conn.fetchval(
            """
            SELECT user_id FROM orders
            GROUP BY user_id ORDER BY count(*) DESC LIMIT 1
            """
        )
        return {k: int(v) for k, v in ids.items()}
    finally:
        await conn.close()


class QueryRecorder:
    """Records every SQL statement the engine executes.

    Used to characterise the planted pathologies: an N+1 is a claim about the
    number of round trips, so the test that guards it has to count round trips.
    This is a test helper, not a preview of the Aperture SDK - it uses the same
    SQLAlchemy event that the SDK will later use, which is itself worth
    knowing works before Day 3 depends on it.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __len__(self) -> int:
        return len(self.statements)

    def matching(self, needle: str) -> list[str]:
        lowered = needle.lower()
        return [s for s in self.statements if lowered in s.lower()]

    def count_matching(self, needle: str) -> int:
        return len(self.matching(needle))

    def summary(self) -> str:
        return "\n".join(f"  {i + 1}. {s[:120]}" for i, s in enumerate(self.statements))


@contextmanager
def _recording() -> Iterator[QueryRecorder]:
    from sqlalchemy import event

    from shop.db import get_engine

    recorder = QueryRecorder()
    sync_engine = get_engine().sync_engine

    def _before_cursor_execute(  # noqa: ANN202
        conn, cursor, statement, parameters, context, executemany
    ):
        recorder.statements.append(" ".join(statement.split()))

    event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield recorder
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before_cursor_execute)


@pytest.fixture
def queries(app) -> Iterator[QueryRecorder]:  # noqa: ANN001
    """Capture the SQL issued during one test."""
    with _recording() as recorder:
        yield recorder


@pytest.fixture
def record_queries(app):  # noqa: ANN001, ANN201
    """Capture SQL for one block, so a test can compare two separate calls."""
    return _recording
