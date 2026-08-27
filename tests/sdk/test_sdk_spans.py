"""The span model, the tracer, nesting, and the per-trace budget."""

from __future__ import annotations

import asyncio
import time

import pytest

from aperture.buffer import SpanBuffer
from aperture.context import (
    TraceState,
    get_current_span_context,
    reset_trace_state,
    set_trace_state,
)
from aperture.spans import (
    UNKNOWN_ROWS,
    SpanKind,
    SpanStatus,
    clock_anchor,
)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_span_carries_every_documented_field(tracer_factory) -> None:
    """The fields DESIGN.md 5.2 stores in ClickHouse must all exist."""
    tracer, buffer = tracer_factory()
    span = tracer.start_span("db.select", SpanKind.CLIENT)
    assert span is not None

    span.db_statement = "SELECT 1"
    span.db_fingerprint = 1234
    span.db_fingerprint_method = "placeholder/x"
    span.db_rows = 7
    span.pool_wait_ns = 500
    span.code_location = "shop/routers/orders.py:95"
    tracer.set_attribute(span, "db.system", "postgresql")
    tracer.end_span(span, status=SpanStatus.OK)

    (recorded,) = buffer.drain_all()
    assert recorded.trace_id > 0
    assert recorded.span_id > 0
    assert recorded.parent_span_id == 0
    assert recorded.service == "test-service"
    assert recorded.operation == "db.select"
    assert recorded.kind is SpanKind.CLIENT
    assert recorded.start_unix_ns > 0
    assert recorded.duration_ns >= 0
    assert recorded.status is SpanStatus.OK
    assert recorded.db_statement == "SELECT 1"
    assert recorded.db_fingerprint == 1234
    assert recorded.db_rows == 7
    assert recorded.pool_wait_ns == 500
    assert recorded.code_location == "shop/routers/orders.py:95"
    assert recorded.attributes["db.system"] == "postgresql"


def test_unknown_row_count_is_distinct_from_zero(tracer_factory) -> None:
    """"No rows" and "could not tell" must not be the same value."""
    tracer, buffer = tracer_factory()
    span = tracer.start_span("db.select", SpanKind.CLIENT)
    tracer.end_span(span)
    (recorded,) = buffer.drain_all()
    assert recorded.db_rows == UNKNOWN_ROWS
    assert UNKNOWN_ROWS != 0


def test_duration_is_measured_monotonically(tracer_factory) -> None:
    tracer, buffer = tracer_factory()
    span = tracer.start_span("slow")
    time.sleep(0.02)
    tracer.end_span(span)
    (recorded,) = buffer.drain_all()
    assert 15_000_000 <= recorded.duration_ns < 500_000_000


def test_ending_a_none_span_is_a_no_op(tracer_factory) -> None:
    """Hooks treat None as 'not instrumented' and must be able to pass it back."""
    tracer, buffer = tracer_factory()
    tracer.end_span(None)
    assert len(buffer) == 0


# ---------------------------------------------------------------------------
# Clock resolution — the reason span timestamps are not time.time_ns()
# ---------------------------------------------------------------------------


def test_span_timestamps_have_sub_millisecond_resolution(tracer_factory) -> None:
    """Consecutive spans must get distinguishable start times.

    On Windows `time.time_ns()` has 15.6 ms resolution, which would give
    twenty sibling queries in a single request identical timestamps and make
    an N+1 indistinguishable from a concurrent fan-out. Span times are derived
    from the monotonic counter for exactly this reason.
    """
    tracer, buffer = tracer_factory()
    for i in range(50):
        span = tracer.start_span(f"op-{i}")
        tracer.end_span(span)
    starts = [s.start_unix_ns for s in buffer.drain_all()]

    assert len(set(starts)) == len(starts), "span start times are not distinguishable"
    assert starts == sorted(starts), "span start times are not monotonic"


def test_span_timestamps_are_close_to_wall_clock(tracer_factory) -> None:
    """Anchored to real time, so a stored span is not from 1970."""
    tracer, buffer = tracer_factory()
    span = tracer.start_span("now")
    tracer.end_span(span)
    (recorded,) = buffer.drain_all()

    wall_now = time.time_ns()
    # Generous: the anchor was taken at import, and drift over a test session
    # is small but not zero.
    assert abs(recorded.start_unix_ns - wall_now) < 60_000_000_000


def test_clock_anchor_is_exposed(tracer_factory) -> None:
    wall, perf = clock_anchor()
    assert wall > 0 and perf > 0


# ---------------------------------------------------------------------------
# Nesting
# ---------------------------------------------------------------------------


def test_nested_spans_form_a_parent_child_chain(tracer_factory) -> None:
    tracer, buffer = tracer_factory()
    with tracer.span("outer") as outer:
        with tracer.span("middle") as middle:
            with tracer.span("inner") as inner:
                assert inner is not None
            assert middle is not None
        assert outer is not None

    spans = {s.operation: s for s in buffer.drain_all()}
    assert spans["inner"].parent_span_id == spans["middle"].span_id
    assert spans["middle"].parent_span_id == spans["outer"].span_id
    assert spans["outer"].parent_span_id == 0
    assert len({s.trace_id for s in spans.values()}) == 1


def test_siblings_share_a_parent_and_do_not_chain(tracer_factory) -> None:
    """The structure the N+1 detector depends on.

    Twenty queries in a loop must come out as twenty siblings under the
    request span, not a twenty-deep chain. If DB spans were made 'current'
    while open, each would parent the next and the common-ancestor grouping
    in DESIGN.md 6.2 would find groups of one.
    """
    tracer, buffer = tracer_factory()
    with tracer.span("request") as parent:
        assert parent is not None
        for i in range(20):
            child = tracer.start_span(f"db.select.{i}", SpanKind.CLIENT)
            tracer.end_span(child)

    spans = buffer.drain_all()
    request = next(s for s in spans if s.operation == "request")
    children = [s for s in spans if s.operation.startswith("db.select")]

    assert len(children) == 20
    assert all(c.parent_span_id == request.span_id for c in children)
    assert len({c.span_id for c in children}) == 20


def test_the_context_manager_restores_the_previous_span(tracer_factory) -> None:
    tracer, _ = tracer_factory()
    assert get_current_span_context() is None
    with tracer.span("outer"):
        assert get_current_span_context() is not None
    assert get_current_span_context() is None


def test_an_exception_marks_the_span_and_propagates(tracer_factory) -> None:
    tracer, buffer = tracer_factory()
    with pytest.raises(ValueError, match="boom"):
        with tracer.span("failing"):
            raise ValueError("boom")

    (recorded,) = buffer.drain_all()
    assert recorded.status is SpanStatus.ERROR
    assert "boom" in recorded.status_message


async def test_nesting_survives_await_boundaries(tracer_factory) -> None:
    tracer, buffer = tracer_factory()

    async def inner_work() -> None:
        await asyncio.sleep(0)
        with tracer.span("inner"):
            await asyncio.sleep(0)

    with tracer.span("outer"):
        await inner_work()

    spans = {s.operation: s for s in buffer.drain_all()}
    assert spans["inner"].parent_span_id == spans["outer"].span_id


# ---------------------------------------------------------------------------
# Per-trace span budget (C3)
# ---------------------------------------------------------------------------


def test_span_budget_caps_a_runaway_trace(tracer_factory) -> None:
    tracer, buffer = tracer_factory(max_spans_per_trace=10)
    state = TraceState(endpoint="/api/runaway")
    token = set_trace_state(state)
    try:
        created = [tracer.start_span(f"db.{i}") for i in range(50)]
    finally:
        reset_trace_state(token)

    recorded = [s for s in created if s is not None]
    assert len(recorded) == 10
    assert state.over_budget == 40
    assert tracer.stats()["spans_dropped_over_budget"] == 40


def test_budget_is_per_trace_not_per_process(tracer_factory) -> None:
    tracer, _ = tracer_factory(max_spans_per_trace=3)
    for _ in range(3):
        state = TraceState()
        token = set_trace_state(state)
        try:
            made = [tracer.start_span("x") for _ in range(5)]
        finally:
            reset_trace_state(token)
        assert len([s for s in made if s is not None]) == 3


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def test_head_sampling_defaults_to_capturing_everything(tracer_factory) -> None:
    """DESIGN.md 7.1.5: head sampling loses the rare slow request."""
    tracer, _ = tracer_factory()
    assert tracer.config.head_sample_rate == 1.0


def test_zero_sample_rate_records_nothing_but_still_returns_state(tracer_factory) -> None:
    tracer, buffer = tracer_factory(head_sample_rate=0.0)
    span, state = tracer.start_root_span("GET /x")
    assert span is None
    assert state is not None
    assert tracer.stats()["spans_dropped_unsampled"] == 1


# ---------------------------------------------------------------------------
# Attribute limits
# ---------------------------------------------------------------------------


def test_attribute_count_is_capped(tracer_factory) -> None:
    tracer, buffer = tracer_factory(max_attributes_per_span=5)
    span = tracer.start_span("op")
    for i in range(20):
        tracer.set_attribute(span, f"key{i}", i)
    tracer.end_span(span)
    (recorded,) = buffer.drain_all()
    assert len(recorded.attributes) == 5


def test_an_existing_attribute_can_still_be_updated_at_the_cap(tracer_factory) -> None:
    tracer, buffer = tracer_factory(max_attributes_per_span=2)
    span = tracer.start_span("op")
    tracer.set_attribute(span, "a", 1)
    tracer.set_attribute(span, "b", 2)
    tracer.set_attribute(span, "c", 3)   # refused
    tracer.set_attribute(span, "a", 99)  # allowed: already present
    tracer.end_span(span)
    (recorded,) = buffer.drain_all()
    assert recorded.attributes == {"a": "99", "b": "2"}


def test_attribute_values_are_truncated(tracer_factory) -> None:
    tracer, buffer = tracer_factory(max_attribute_chars=16)
    span = tracer.start_span("op")
    tracer.set_attribute(span, "big", "x" * 1000)
    tracer.end_span(span)
    (recorded,) = buffer.drain_all()
    assert len(recorded.attributes["big"]) == 16


def test_an_attribute_whose_str_raises_is_skipped(tracer_factory) -> None:
    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    tracer, buffer = tracer_factory()
    span = tracer.start_span("op")
    tracer.set_attribute(span, "bad", Hostile())
    tracer.end_span(span)
    (recorded,) = buffer.drain_all()
    assert "bad" not in recorded.attributes


def test_setting_an_attribute_on_none_is_a_no_op(tracer_factory) -> None:
    tracer, _ = tracer_factory()
    tracer.set_attribute(None, "k", "v")  # must not raise


# ---------------------------------------------------------------------------
# Buffer interaction
# ---------------------------------------------------------------------------


def test_finished_spans_go_to_the_buffer_and_overflow_is_counted(tracer_factory) -> None:
    tracer, buffer = tracer_factory(buffer_capacity=5)
    for i in range(12):
        span = tracer.start_span(f"op{i}")
        tracer.end_span(span)

    assert tracer.stats()["spans_finished"] == 12
    assert buffer.stats()["spans_dropped_buffer_full"] == 7
    assert len(buffer) == 5
