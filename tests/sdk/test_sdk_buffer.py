"""The bounded ring buffer — where design constraint C3 is enforced."""

from __future__ import annotations

import threading

import pytest

from aperture.buffer import SpanBuffer
from aperture.spans import Span, SpanKind


def make_span(n: int = 0) -> Span:
    return Span(
        trace_id=1,
        span_id=n or 1,
        parent_span_id=0,
        service="test",
        operation=f"op-{n}",
        kind=SpanKind.INTERNAL,
        start_unix_ns=n,
    )


def test_rejects_a_zero_capacity() -> None:
    with pytest.raises(ValueError):
        SpanBuffer(0)


def test_put_and_drain_preserve_order() -> None:
    buffer = SpanBuffer(8)
    for i in range(5):
        assert buffer.put(make_span(i)) is True
    drained = buffer.drain(10)
    assert [s.operation for s in drained] == [f"op-{i}" for i in range(5)]
    assert len(buffer) == 0


def test_drain_respects_its_limit() -> None:
    buffer = SpanBuffer(8)
    for i in range(8):
        buffer.put(make_span(i))
    assert len(buffer.drain(3)) == 3
    assert len(buffer) == 5


def test_draining_an_empty_buffer_returns_nothing() -> None:
    assert SpanBuffer(4).drain(10) == []


def test_ring_wraps_around_without_losing_order() -> None:
    """Exercise the modular indices: fill, drain, refill past the wrap point."""
    buffer = SpanBuffer(4)
    for i in range(4):
        buffer.put(make_span(i))
    buffer.drain(3)
    for i in range(4, 7):
        buffer.put(make_span(i))
    drained = buffer.drain(10)
    assert [s.operation for s in drained] == ["op-3", "op-4", "op-5", "op-6"]


# ---------------------------------------------------------------------------
# Overflow — the C3 requirement
# ---------------------------------------------------------------------------


def test_overflow_drops_and_counts_instead_of_growing() -> None:
    buffer = SpanBuffer(4)
    accepted = [buffer.put(make_span(i)) for i in range(10)]

    assert accepted == [True] * 4 + [False] * 6
    assert len(buffer) == 4, "the buffer grew past its capacity"
    assert buffer.capacity == 4
    assert buffer.dropped_buffer_full == 6
    assert buffer.accepted == 4


def test_overflow_keeps_the_oldest_spans() -> None:
    """Drop-newest, like OpenTelemetry's own BatchSpanProcessor.

    Evicting the oldest instead would keep fresher data but shred traces that
    are already half-collected, and the structural detectors reason about
    complete trees.
    """
    buffer = SpanBuffer(3)
    for i in range(6):
        buffer.put(make_span(i))
    assert [s.operation for s in buffer.drain_all()] == ["op-0", "op-1", "op-2"]


def test_buffer_recovers_after_being_drained() -> None:
    buffer = SpanBuffer(2)
    for i in range(5):
        buffer.put(make_span(i))
    assert buffer.dropped_buffer_full == 3

    buffer.drain_all()
    assert buffer.put(make_span(99)) is True
    assert len(buffer) == 1


def test_put_never_raises_and_always_returns_a_bool() -> None:
    buffer = SpanBuffer(1)
    assert buffer.put(make_span(1)) is True
    assert buffer.put(make_span(2)) is False


def test_drained_slots_release_their_references() -> None:
    """A drained slot must not keep pointing at the span.

    Otherwise the ring holds `capacity` spans alive forever after the traffic
    that produced them is long gone — a bounded leak, but a leak. Checked by
    inspecting the slots directly rather than with `weakref`, because `Span`
    uses `slots=True` and is deliberately not weak-referenceable: adding
    `__weakref__` would add a pointer to every span in the system to satisfy
    a test.
    """
    buffer = SpanBuffer(4)
    for i in range(3):
        buffer.put(make_span(i))

    assert sum(slot is not None for slot in buffer._slots) == 3
    buffer.drain_all()
    assert all(slot is None for slot in buffer._slots)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_producers_lose_nothing() -> None:
    """Multi-producer, single-consumer: accepted + dropped must equal offered."""
    buffer = SpanBuffer(500)
    threads_count = 8
    per_thread = 500
    drained: list = []
    stop = threading.Event()

    def produce() -> None:
        for i in range(per_thread):
            buffer.put(make_span(i))

    def consume() -> None:
        while not stop.is_set():
            drained.extend(buffer.drain(64))
        drained.extend(buffer.drain_all())

    consumer = threading.Thread(target=consume)
    consumer.start()
    producers = [threading.Thread(target=produce) for _ in range(threads_count)]
    for t in producers:
        t.start()
    for t in producers:
        t.join()
    stop.set()
    consumer.join()

    offered = threads_count * per_thread
    assert buffer.accepted + buffer.dropped_buffer_full == offered
    assert len(drained) == buffer.accepted
    assert len(buffer) == 0


def test_peak_size_is_tracked() -> None:
    buffer = SpanBuffer(10)
    for i in range(7):
        buffer.put(make_span(i))
    buffer.drain(7)
    for i in range(2):
        buffer.put(make_span(i))
    assert buffer.stats()["buffer_peak_size"] == 7


def test_stats_expose_the_drop_counter() -> None:
    buffer = SpanBuffer(2)
    for i in range(5):
        buffer.put(make_span(i))
    stats = buffer.stats()
    assert stats["buffer_capacity"] == 2
    assert stats["buffer_size"] == 2
    assert stats["spans_buffered"] == 2
    assert stats["spans_dropped_buffer_full"] == 3
