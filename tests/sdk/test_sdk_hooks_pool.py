"""Connection pool instrumentation — the measurement detector D3 stands on.

D3's whole job is telling "the database is slow" apart from "requests are
queuing for a connection while the database sits idle". Those look identical
unless connection-acquisition time is measured separately from execution time,
so these tests are about that number being real: produced by an actual wait on
an actual exhausted pool, not inferred.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import aperture
from aperture.context import TraceState, reset_trace_state, set_trace_state
from aperture.hooks import pool as pool_hook
from aperture.spans import CLIENT_KIND_DB


@pytest.fixture
async def tiny_pool_engine(seeded_database):
    """A pool of exactly one connection, so contention is easy to create."""
    url = seeded_database.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=30)
    try:
        yield engine
    finally:
        await engine.dispose()


def db_spans(spans) -> list:
    return [s for s in spans if s.attributes.get("aperture.client_kind") == CLIENT_KIND_DB]


async def test_checkouts_and_checkins_are_counted(instrumented, tiny_pool_engine) -> None:
    instrumented()
    async with tiny_pool_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    stats = pool_hook.stats()
    assert stats["pool_checkouts"] >= 1
    assert stats["pool_checkins"] >= 1
    assert stats["pool_new_connections"] >= 1
    aperture.drain_buffered_spans()


async def test_hold_time_is_measured(instrumented, tiny_pool_engine) -> None:
    """`mean_hold_time` is the second term in D3's Little's Law recommendation."""
    instrumented()
    async with tiny_pool_engine.connect() as conn:
        await conn.execute(text("SELECT pg_sleep(0.05)"))

    assert pool_hook.stats()["pool_mean_hold_ns"] > 40_000_000
    aperture.drain_buffered_spans()


async def test_contention_produces_real_measured_wait(
    instrumented, tiny_pool_engine
) -> None:
    """Two concurrent users, one connection: the second genuinely queues.

    The wait is created by an exhausted pool rather than simulated, which is
    the only way this number means anything.
    """
    instrumented()

    # Warm the pool so the first connect's TCP handshake is not counted as
    # queueing time; we want the wait produced by contention, not by dialing.
    async with tiny_pool_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    pool_hook.reset_stats()
    aperture.drain_buffered_spans()

    async def worker() -> None:
        async with tiny_pool_engine.connect() as conn:
            await conn.execute(text("SELECT pg_sleep(0.15)"))

    await asyncio.gather(worker(), worker())

    stats = pool_hook.stats()
    # The loser of the race waited for the winner's 150 ms query.
    assert stats["pool_max_wait_ns"] > 100_000_000, stats
    assert stats["pool_slow_checkouts"] >= 1
    aperture.drain_buffered_spans()


async def test_the_wait_lands_on_the_first_span_of_the_checkout(
    instrumented, tiny_pool_engine
) -> None:
    """Attributed once, not once per query.

    D3 sums `pool_wait_ns` across spans. Copying the checkout's wait onto every
    statement that runs on that connection would multiply the numerator by the
    query count and make a healthy pool look saturated.
    """
    instrumented()
    async with tiny_pool_engine.connect() as conn:
        for _ in range(4):
            await conn.execute(text("SELECT id FROM users LIMIT 1"))

    spans = sorted(db_spans(aperture.drain_buffered_spans()), key=lambda s: s.start_unix_ns)
    assert len(spans) == 4
    assert sum(1 for s in spans if s.pool_wait_ns > 0) <= 1
    assert all(s.pool_wait_ns == 0 for s in spans[1:])


async def test_request_total_wait_accumulates_on_the_trace(
    instrumented, tiny_pool_engine
) -> None:
    """The request-level figure is the sum over every checkout it made."""
    instrumented()
    state = TraceState(endpoint="/api/thing")
    token = set_trace_state(state)
    try:
        for _ in range(3):
            async with tiny_pool_engine.connect() as conn:
                await conn.execute(text("SELECT id FROM users LIMIT 1"))
    finally:
        reset_trace_state(token)

    spans = db_spans(aperture.drain_buffered_spans())
    assert state.pool_wait_ns == sum(s.pool_wait_ns for s in spans)
    assert state.pool_wait_ns > 0


async def test_a_returned_connection_does_not_carry_stale_wait(
    instrumented, tiny_pool_engine
) -> None:
    """A checkout that runs no statement must not leak its wait into the next."""
    instrumented()
    async with tiny_pool_engine.connect():
        pass  # checked out, never used
    async with tiny_pool_engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 1"))

    spans = db_spans(aperture.drain_buffered_spans())
    assert len(spans) == 1
    # Whatever this span reports is its own checkout's wait, not the unused
    # one's, so it must be far below the first connect's handshake cost.
    assert spans[0].pool_wait_ns < 50_000_000


async def test_connect_is_restored_on_uninstall(seeded_database) -> None:
    """The class patch must be reversible, or tests leak into each other."""
    from sqlalchemy.pool import Pool

    original = Pool.connect
    aperture.instrument(
        aperture.ApertureConfig(enabled=True, collector_endpoint="127.0.0.1:14317")
    )
    assert Pool.connect is not original

    aperture.shutdown(timeout=0.5)
    assert Pool.connect is original


async def test_a_failing_connect_propagates_untouched(instrumented) -> None:
    """Our timing wrapper must not swallow the application's connection error."""
    instrumented()
    engine = create_async_engine(
        "postgresql+asyncpg://nobody:wrong@127.0.0.1:5433/nope",
        pool_size=1,
        connect_args={"timeout": 2},
    )
    try:
        with pytest.raises(Exception) as excinfo:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        assert "nope" in str(excinfo.value).lower() or "nobody" in str(excinfo.value).lower()
    finally:
        await engine.dispose()
    aperture.drain_buffered_spans()


async def test_stats_are_exposed_through_the_sdk(instrumented, tiny_pool_engine) -> None:
    instrumented()
    async with tiny_pool_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    stats = aperture.get_stats()
    assert "pool_checkouts" in stats
    assert "pool_mean_wait_ns" in stats
    assert "pool_slow_checkouts" in stats
    aperture.drain_buffered_spans()
