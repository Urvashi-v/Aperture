"""Trace context: identifiers, W3C propagation, and contextvar behaviour."""

from __future__ import annotations

import asyncio

import pytest

from aperture.context import (
    FLAG_SAMPLED,
    SpanContext,
    TraceState,
    extract_from_headers,
    format_traceparent,
    generate_span_id,
    generate_trace_id,
    get_current_span_context,
    get_trace_state,
    inject_into_headers,
    new_child_context,
    parse_traceparent,
    reset_span_context,
    reset_trace_state,
    set_current_span_context,
    set_trace_state,
)


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def test_trace_ids_are_128_bit_and_never_zero() -> None:
    for _ in range(2000):
        value = generate_trace_id()
        assert 0 < value < 2**128


def test_span_ids_are_64_bit_and_never_zero() -> None:
    for _ in range(2000):
        value = generate_span_id()
        assert 0 < value < 2**64


def test_ids_do_not_collide_in_bulk() -> None:
    assert len({generate_trace_id() for _ in range(10_000)}) == 10_000


def test_hex_rendering_is_fixed_width() -> None:
    ctx = SpanContext(trace_id=1, span_id=2, parent_span_id=0, sampled=True)
    assert ctx.hex_trace_id() == "0" * 31 + "1"
    assert ctx.hex_span_id() == "0" * 15 + "2"


# ---------------------------------------------------------------------------
# W3C traceparent
# ---------------------------------------------------------------------------


def test_traceparent_round_trip() -> None:
    original = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=0,
        sampled=True,
    )
    parsed = parse_traceparent(format_traceparent(original))

    assert parsed is not None
    assert parsed.trace_id == original.trace_id
    # The inbound span becomes our parent; this process mints a new span id.
    assert parsed.parent_span_id == original.span_id
    assert parsed.span_id != original.span_id
    assert parsed.sampled is True
    assert parsed.remote is True


def test_traceparent_format_matches_the_spec() -> None:
    ctx = SpanContext(trace_id=0xABC, span_id=0xDEF, parent_span_id=0, sampled=True)
    header = format_traceparent(ctx)
    version, trace, span, flags = header.split("-")
    assert version == "00"
    assert len(trace) == 32 and len(span) == 16 and len(flags) == 2
    assert int(flags, 16) & FLAG_SAMPLED


def test_unsampled_flag_is_carried() -> None:
    ctx = SpanContext(trace_id=1234, span_id=99, parent_span_id=0, sampled=False)
    parsed = parse_traceparent(format_traceparent(ctx))
    assert parsed is not None
    assert parsed.sampled is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "garbage",
        "00-tooshort-0000000000000001-01",
        "00-" + "0" * 32 + "-" + "0" * 16 + "-01",       # all-zero ids
        "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",       # forbidden version
        "00-" + "z" * 32 + "-" + "b" * 16 + "-01",       # non-hex
        "00-" + "a" * 32,                                 # truncated
        "00-" + "a" * 31 + "-" + "b" * 16 + "-01",       # wrong length
    ],
)
def test_malformed_traceparent_is_ignored_not_raised(header) -> None:
    """A broken upstream header starts a new trace; it never fails a request."""
    assert parse_traceparent(header) is None


def test_future_versions_are_accepted_best_effort() -> None:
    """The spec requires tolerating unknown versions that keep the prefix."""
    parsed = parse_traceparent("01-" + "a" * 32 + "-" + "b" * 16 + "-01-extra")
    assert parsed is not None
    assert parsed.trace_id == int("a" * 32, 16)


def test_header_extraction_and_injection() -> None:
    ctx = SpanContext(trace_id=77, span_id=88, parent_span_id=0, sampled=True)
    headers: dict[str, str] = {}
    inject_into_headers(headers, ctx)
    assert "traceparent" in headers

    extracted = extract_from_headers(headers)
    assert extracted is not None
    assert extracted.trace_id == 77


# ---------------------------------------------------------------------------
# Child derivation
# ---------------------------------------------------------------------------


def test_child_inherits_trace_and_points_at_parent() -> None:
    parent = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=0,
        sampled=True,
    )
    child = new_child_context(parent)
    assert child.trace_id == parent.trace_id
    assert child.parent_span_id == parent.span_id
    assert child.span_id not in (parent.span_id, 0)


def test_child_inherits_the_sampling_decision() -> None:
    """A trace must never be half-recorded: the detectors read whole trees."""
    parent = SpanContext(trace_id=5, span_id=6, parent_span_id=0, sampled=False)
    assert new_child_context(parent).sampled is False


def test_child_with_no_parent_starts_a_new_trace() -> None:
    ctx = new_child_context(None)
    assert ctx.parent_span_id == 0
    assert ctx.trace_id != 0


# ---------------------------------------------------------------------------
# contextvar propagation — the property everything else depends on
# ---------------------------------------------------------------------------


async def test_context_survives_await_boundaries() -> None:
    ctx = SpanContext(trace_id=42, span_id=1, parent_span_id=0, sampled=True)
    token = set_current_span_context(ctx)
    try:

        async def level_three() -> int:
            await asyncio.sleep(0)
            seen = get_current_span_context()
            assert seen is not None
            return seen.trace_id

        async def level_two() -> int:
            await asyncio.sleep(0)
            return await level_three()

        assert await level_two() == 42
    finally:
        reset_span_context(token)


async def test_context_propagates_into_gathered_tasks() -> None:
    """asyncio.gather copies the context into each child task."""
    ctx = SpanContext(trace_id=4242, span_id=1, parent_span_id=0, sampled=True)
    token = set_current_span_context(ctx)
    try:

        async def read() -> int:
            await asyncio.sleep(0)
            current = get_current_span_context()
            return current.trace_id if current else 0

        results = await asyncio.gather(*(read() for _ in range(8)))
        assert results == [4242] * 8
    finally:
        reset_span_context(token)


async def test_a_child_task_cannot_leak_its_context_upwards() -> None:
    """Setting a contextvar inside a task must not affect the parent.

    This is why per-request accumulators live on a mutable TraceState object
    rather than in contextvars of their own.
    """
    outer = SpanContext(trace_id=1, span_id=1, parent_span_id=0, sampled=True)
    token = set_current_span_context(outer)
    try:

        async def clobber() -> None:
            set_current_span_context(
                SpanContext(trace_id=999, span_id=2, parent_span_id=0, sampled=True)
            )

        await asyncio.gather(clobber())
        still = get_current_span_context()
        assert still is not None and still.trace_id == 1
    finally:
        reset_span_context(token)


async def test_trace_state_mutations_are_visible_across_tasks() -> None:
    """The counterpart: mutating the shared object *does* propagate.

    The span budget and the pool-wait accumulator are incremented deep inside
    a request and read by the middleware at the end, frequently across a task
    boundary introduced by other middleware. This is the mechanism that makes
    that work.
    """
    state = TraceState(endpoint="/api/things")
    token = set_trace_state(state)
    try:

        async def do_work() -> None:
            await asyncio.sleep(0)
            current = get_trace_state()
            assert current is not None
            current.span_count += 5
            current.pool_wait_ns += 1_000

        await asyncio.gather(do_work(), do_work())
        assert state.span_count == 10
        assert state.pool_wait_ns == 2_000
    finally:
        reset_trace_state(token)


def test_reset_tolerates_a_foreign_token() -> None:
    """Resetting a token minted in another context must not raise.

    A span can legitimately be opened in one task and closed in another; an
    exception thrown from a `finally` in the request path would be far worse
    than losing the restore.
    """
    import contextvars

    ctx = contextvars.copy_context()
    token = ctx.run(
        set_current_span_context,
        SpanContext(trace_id=1, span_id=1, parent_span_id=0, sampled=True),
    )
    reset_span_context(token)  # must not raise
    assert get_current_span_context() is None


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


def test_endpoint_resolver_is_called_once_then_cached() -> None:
    calls = {"n": 0}

    def resolver() -> str:
        calls["n"] += 1
        return "/api/products/{product_id}"

    state = TraceState(endpoint_resolver=resolver)
    assert state.resolve_endpoint() == "/api/products/{product_id}"
    assert state.resolve_endpoint() == "/api/products/{product_id}"
    assert calls["n"] == 1
    assert state.endpoint_resolver is None


def test_endpoint_resolver_that_returns_nothing_is_retried() -> None:
    """Before dispatch the route is unknown; the next span should try again."""
    answers = iter(["", "", "/api/orders"])
    state = TraceState(endpoint_resolver=lambda: next(answers))
    assert state.resolve_endpoint() == ""
    assert state.resolve_endpoint() == ""
    assert state.resolve_endpoint() == "/api/orders"


def test_endpoint_resolver_that_raises_is_swallowed() -> None:
    def boom() -> str:
        raise RuntimeError("router exploded")

    state = TraceState(endpoint_resolver=boom)
    assert state.resolve_endpoint() == ""
