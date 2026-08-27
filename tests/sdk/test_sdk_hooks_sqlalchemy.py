"""SQLAlchemy instrumentation, against a real PostgreSQL.

No fake engine and no mocked cursor. Row counts come from the actual driver,
the greenlet boundary that makes code-location capture hard is genuinely
present, and the queries are the ones the benchmark application runs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

import aperture
from aperture.context import TraceState, reset_trace_state, set_trace_state
from aperture.hooks import (
    FINGERPRINT_METHOD_PLACEHOLDER,
    placeholder_fingerprint,
)
from aperture.spans import CLIENT_KIND_DB, SpanKind, SpanStatus, UNKNOWN_ROWS


@pytest.fixture
async def engine(seeded_database):
    """An async engine on the test database, disposed after each test."""
    url = seeded_database.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, pool_size=2, max_overflow=1)
    try:
        yield engine
    finally:
        await engine.dispose()


def db_spans(spans) -> list:
    return [s for s in spans if s.attributes.get("aperture.client_kind") == CLIENT_KIND_DB]


# ---------------------------------------------------------------------------
# Basic capture
# ---------------------------------------------------------------------------


async def test_a_query_produces_one_client_span(instrumented, engine) -> None:
    instrumented()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 3"))

    spans = db_spans(aperture.drain_buffered_spans())
    assert len(spans) == 1
    span = spans[0]
    assert span.kind is SpanKind.CLIENT
    assert span.status is SpanStatus.OK
    assert span.operation == "db.select"
    assert span.duration_ns > 0
    assert span.attributes["db.operation"] == "SELECT"
    assert span.attributes["db.system"] == "postgresql"


async def test_the_statement_text_is_captured(instrumented, engine) -> None:
    instrumented()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users WHERE id = 1"))

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert "FROM users" in span.db_statement


async def test_statement_capture_can_be_switched_off(instrumented, engine) -> None:
    instrumented(capture_db_statement=False)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 1"))

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert span.db_statement == ""
    # The fingerprint still works, so grouping survives even when the text does not.
    assert span.db_fingerprint != 0


async def test_long_statements_are_truncated(instrumented, engine) -> None:
    instrumented(max_statement_chars=32)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id, email, display_name FROM users LIMIT 5"))

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert len(span.db_statement) == 32


async def test_row_counts_are_recorded(instrumented, engine) -> None:
    instrumented()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 4"))

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert span.db_rows == 4
    assert span.db_rows != UNKNOWN_ROWS


async def test_row_count_of_an_empty_result_is_zero_not_unknown(
    instrumented, engine
) -> None:
    """The N+1 row ceiling has to tell 'no rows' from 'could not tell'."""
    instrumented()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users WHERE id = -1"))

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert span.db_rows == 0


# ---------------------------------------------------------------------------
# Fingerprint (placeholder) and parameters
# ---------------------------------------------------------------------------


async def test_the_fingerprint_is_labelled_as_a_placeholder(
    instrumented, engine
) -> None:
    """Nothing downstream may mistake this for the sqlglot fingerprint."""
    instrumented()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 1"))

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert span.db_fingerprint != 0
    assert span.db_fingerprint_method == FINGERPRINT_METHOD_PLACEHOLDER
    assert "placeholder" in span.db_fingerprint_method


async def test_repeated_statements_share_a_fingerprint(instrumented, engine) -> None:
    """The property the N+1 detector groups on."""
    instrumented()
    async with engine.connect() as conn:
        for user_id in (1, 2, 3, 4, 5):
            await conn.execute(
                text("SELECT id FROM users WHERE id = :uid"), {"uid": user_id}
            )

    spans = db_spans(aperture.drain_buffered_spans())
    assert len(spans) == 5
    assert len({s.db_fingerprint for s in spans}) == 1


async def test_different_statements_get_different_fingerprints(
    instrumented, engine
) -> None:
    instrumented()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 1"))
        await conn.execute(text("SELECT id FROM products LIMIT 1"))

    spans = db_spans(aperture.drain_buffered_spans())
    assert len({s.db_fingerprint for s in spans}) == 2


def test_the_placeholder_fingerprint_normalises_whitespace() -> None:
    assert placeholder_fingerprint("SELECT  1\n  FROM t") == placeholder_fingerprint(
        "SELECT 1 FROM t"
    )


def test_the_placeholder_fingerprint_does_not_collapse_literals() -> None:
    """Stated as a test so the limitation cannot be forgotten.

    The real fingerprint (DESIGN.md 6.1, sqlglot, Week 2 Day 9) must make
    these equal. This one does not, and the `db_fingerprint_method` field is
    what stops that from being a silent lie.
    """
    a = placeholder_fingerprint("SELECT * FROM posts WHERE author_id = 42")
    b = placeholder_fingerprint("SELECT * FROM posts WHERE author_id = 77")
    assert a != b


async def test_varying_parameters_produce_varying_digests(
    instrumented, engine
) -> None:
    """D1 separates an N+1 from a caching problem using parameter variance."""
    instrumented()
    async with engine.connect() as conn:
        for user_id in (1, 2, 3):
            await conn.execute(
                text("SELECT id FROM users WHERE id = :uid"), {"uid": user_id}
            )

    digests = {
        s.attributes["aperture.db.params_digest"]
        for s in db_spans(aperture.drain_buffered_spans())
    }
    assert len(digests) == 3


async def test_identical_parameters_produce_one_digest(instrumented, engine) -> None:
    instrumented()
    async with engine.connect() as conn:
        for _ in range(4):
            await conn.execute(
                text("SELECT id FROM users WHERE id = :uid"), {"uid": 1}
            )

    digests = {
        s.attributes["aperture.db.params_digest"]
        for s in db_spans(aperture.drain_buffered_spans())
    }
    assert len(digests) == 1


async def test_parameter_values_are_never_stored(instrumented, engine) -> None:
    """Only a digest. Bound parameters are customer data."""
    instrumented()
    secret = "correct-horse-battery-staple"
    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": secret}
        )

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert secret not in repr(span)


# ---------------------------------------------------------------------------
# Code location
# ---------------------------------------------------------------------------


async def test_the_call_site_is_captured_through_the_greenlet_boundary(
    instrumented, engine
) -> None:
    """Async SQLAlchemy runs the DBAPI layer in a greenlet.

    A plain `f_back` walk from the hook ends inside SQLAlchemy - the calling
    application code is on the *parent* greenlet's stack. If this test fails,
    every finding's "site" field is empty and the tool cannot tell anyone
    where their N+1 is.
    """
    instrumented()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 1"))

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert span.code_location, "no application frame found through the greenlet"
    assert "test_sdk_hooks_sqlalchemy.py" in span.code_location
    assert span.attributes["code.function"]


async def test_two_call_sites_with_identical_sql_are_told_apart(
    instrumented, engine
) -> None:
    """Caching by fingerprint would make these report the same location.

    This is the case that made the cache off by default: in sample-shop the
    auth dependency and the product-page N+1 both run
    `SELECT ... FROM users WHERE id = $1`, and a cached location points the
    N+1 finding at the auth dependency.
    """
    instrumented()
    statement = text("SELECT id FROM users WHERE id = :uid")

    async def site_one(conn) -> None:
        await conn.execute(statement, {"uid": 1})

    async def site_two(conn) -> None:
        await conn.execute(statement, {"uid": 2})

    async with engine.connect() as conn:
        await site_one(conn)
        await site_two(conn)

    spans = db_spans(aperture.drain_buffered_spans())
    assert len({s.db_fingerprint for s in spans}) == 1, "expected one fingerprint"
    assert len({s.code_location for s in spans}) == 2, "call sites were conflated"
    assert {s.attributes["code.function"] for s in spans} == {"site_one", "site_two"}


async def test_enabling_the_cache_restores_the_documented_tradeoff(
    instrumented, engine
) -> None:
    """With the cache on, the second call site inherits the first's location."""
    instrumented(code_location_cache=True)
    statement = text("SELECT id FROM users WHERE id = :uid")

    async def first_site(conn) -> None:
        await conn.execute(statement, {"uid": 1})

    async def second_site(conn) -> None:
        await conn.execute(statement, {"uid": 2})

    async with engine.connect() as conn:
        await first_site(conn)
        await second_site(conn)

    spans = db_spans(aperture.drain_buffered_spans())
    assert len({s.code_location for s in spans}) == 1


async def test_code_location_capture_can_be_switched_off(
    instrumented, engine
) -> None:
    instrumented(capture_code_location=False)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 1"))

    (span,) = db_spans(aperture.drain_buffered_spans())
    assert span.code_location == ""


# ---------------------------------------------------------------------------
# Errors and noise
# ---------------------------------------------------------------------------


async def test_a_failing_query_still_closes_its_span(instrumented, engine) -> None:
    """`after_cursor_execute` does not fire on failure.

    Without the `handle_error` hook the span would leak and the trace would be
    missing the operation that broke the request.
    """
    instrumented()
    with pytest.raises(ProgrammingError):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT * FROM table_that_does_not_exist"))

    spans = db_spans(aperture.drain_buffered_spans())
    assert len(spans) == 1
    assert spans[0].status is SpanStatus.ERROR
    assert spans[0].status_message


async def test_transaction_control_statements_are_not_traced(
    instrumented, engine
) -> None:
    """BEGIN/COMMIT would add two spans to every write and tell nobody anything."""
    instrumented()
    async with engine.begin() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 1"))

    spans = aperture.drain_buffered_spans()
    operations = {s.operation for s in spans}
    assert "db.begin" not in operations
    assert "db.commit" not in operations
    assert "db.rollback" not in operations


# ---------------------------------------------------------------------------
# The N+1 shape
# ---------------------------------------------------------------------------


async def test_sibling_queries_form_the_n_plus_one_signature(
    instrumented, engine
) -> None:
    """Every clause of the DESIGN.md 6.2 definition, checked on real spans."""
    instrumented()
    state = TraceState(endpoint="/api/orders")
    token = set_trace_state(state)
    try:
        async with engine.connect() as conn:
            parent_ids = list(
                (await conn.execute(text("SELECT id FROM orders LIMIT 8"))).scalars()
            )
            for order_id in parent_ids:
                await conn.execute(
                    text("SELECT id FROM order_items WHERE order_id = :oid"),
                    {"oid": order_id},
                )
    finally:
        reset_trace_state(token)

    spans = db_spans(aperture.drain_buffered_spans())
    children = [s for s in spans if "order_items" in s.db_statement]
    assert len(children) == 8

    # One fingerprint across all of them.
    assert len({s.db_fingerprint for s in children}) == 1
    # Varying bound parameters, which is what separates this from a cache miss.
    assert len({s.attributes["aperture.db.params_digest"] for s in children}) > 1
    # Sequential, not concurrent: each starts after the previous one ended.
    ordered = sorted(children, key=lambda s: s.start_unix_ns)
    for earlier, later in zip(ordered, ordered[1:]):
        assert later.start_unix_ns >= earlier.start_unix_ns + earlier.duration_ns
    # All siblings of one another.
    assert len({s.parent_span_id for s in children}) == 1
    # And they all know which endpoint they belong to.
    assert {s.endpoint for s in children} == {"/api/orders"}


async def test_the_per_trace_budget_bounds_a_runaway_loop(
    instrumented, engine
) -> None:
    instrumented(max_spans_per_trace=5)
    state = TraceState(endpoint="/api/runaway")
    token = set_trace_state(state)
    try:
        async with engine.connect() as conn:
            for i in range(20):
                await conn.execute(
                    text("SELECT id FROM users WHERE id = :n"), {"n": i + 1}
                )
    finally:
        reset_trace_state(token)

    spans = db_spans(aperture.drain_buffered_spans())
    assert len(spans) <= 5
    assert state.over_budget > 0


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


async def test_queries_keep_working_when_a_hook_raises(
    instrumented, engine, monkeypatch
) -> None:
    """A bug in this SDK must cost telemetry, never the application."""
    instrumented()
    from aperture.hooks import sqlalchemy as hook

    def exploding_fingerprint(_statement: str) -> int:
        raise RuntimeError("bug in the SDK")

    monkeypatch.setattr(hook, "placeholder_fingerprint", exploding_fingerprint)

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id FROM users LIMIT 2"))
        assert len(result.fetchall()) == 2

    # No DB spans, because the hook bailed - but the query returned its rows.
    assert db_spans(aperture.drain_buffered_spans()) == []


async def test_uninstalling_stops_capture_completely(engine) -> None:
    aperture.instrument(
        aperture.ApertureConfig(enabled=True, collector_endpoint="127.0.0.1:14317")
    )
    aperture.shutdown(timeout=0.5)

    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM users LIMIT 1"))

    assert aperture.drain_buffered_spans() == []
    assert not aperture.is_enabled()
