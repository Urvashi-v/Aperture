"""Database engine, connection pool and session lifecycle.

The pool configuration is deliberately explicit rather than left at
SQLAlchemy's defaults. Pool size is an independent variable in the evaluation
(planted pathology P5, connection pool saturation), so it has to be a knob that
is set in exactly one place and read from configuration.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shop.config import Settings, get_settings

logger = logging.getLogger("shop.db")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build a new async engine. Does not touch module state."""
    settings = settings or get_settings()
    return create_async_engine(
        settings.sqlalchemy_url,
        echo=settings.db_echo_sql,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        # Recycle below the typical one-hour idle timeout of managed
        # PostgreSQL, so a stale socket never surfaces as a request error.
        pool_recycle=1800,
        # pre_ping costs one round trip per checkout. It is on because a
        # benchmark that fails with "connection was closed" after an idle
        # period wastes far more time than the ping costs.
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                # Tags every backend in pg_stat_activity, which makes it
                # obvious whether load came from the app, the seeder, or a
                # stray psql session.
                "application_name": "sample-shop",
            }
        },
    )


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create the process-wide engine and session factory (idempotent)."""
    global _engine, _session_factory

    if _engine is None:
        settings = settings or get_settings()
        _engine = create_engine(settings)
        _session_factory = async_sessionmaker(
            _engine,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info(
            "database engine initialised",
            extra={
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "database": settings.postgres_db,
                "host": settings.postgres_host,
                "port": settings.postgres_port,
            },
        )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None  # narrowed by init_engine
    return _session_factory


async def dispose_engine() -> None:
    """Close every pooled connection. Called on application shutdown."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        logger.info("database engine disposed")
    _engine = None
    _session_factory = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    The session is closed when the request finishes, which returns the
    connection to the pool. Requests are read-mostly; the handful of writing
    endpoints commit explicitly, so there is no implicit commit here.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_database(engine: AsyncEngine | None = None) -> dict[str, Any]:
    """Readiness probe: run a trivial query and report pool statistics."""
    engine = engine or get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        result.scalar_one()

    pool = engine.pool
    stats: dict[str, Any] = {"status": "ok"}
    # QueuePool exposes these; NullPool (used in some test setups) does not.
    for name in ("size", "checkedin", "checkedout", "overflow"):
        getter = getattr(pool, name, None)
        if callable(getter):
            try:
                stats[f"pool_{name}"] = getter()
            except (AttributeError, TypeError):  # pragma: no cover - defensive
                pass
    return stats
