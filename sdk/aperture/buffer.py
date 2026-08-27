"""A fixed-size ring buffer for finished spans.

This is where design constraint C3 is actually enforced. The buffer allocates
its slots once and never grows. When it is full it refuses the new span and
increments a counter. It never blocks a producer, never allocates on `put`,
and never raises.

**Drop policy: newest-first.** When the ring is full the *incoming* span is
discarded and the buffered ones are kept, which is what OpenTelemetry's own
`BatchSpanProcessor` does. The alternative — evicting the oldest to make room —
would keep the freshest data but shred traces that are already half-collected,
and the structural detectors reason about complete trees. A trace we drop
entirely costs us one sample; a trace we mangle costs us a false negative or,
worse, a false positive.

**Why a lock and not a lock-free structure.** Producers are request coroutines,
which in a normal asyncio application all run on one thread; the consumer is
the single exporter thread. Contention is therefore near zero, and an
uncontended `threading.Lock` acquire in CPython is a handful of nanoseconds.
A genuinely lock-free ring in pure Python would be slower, not faster, because
every atomic operation would still be mediated by the GIL.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from aperture.spans import Span


class SpanBuffer:
    """Bounded MPSC ring of finished spans."""

    __slots__ = (
        "_capacity",
        "_slots",
        "_head",
        "_tail",
        "_size",
        "_lock",
        "accepted",
        "dropped_buffer_full",
        "peak_size",
    )

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("buffer capacity must be at least 1")
        self._capacity = capacity
        # Allocated once, up front. This list is the buffer's entire memory
        # footprint apart from the spans it points at.
        self._slots: list[Any] = [None] * capacity
        self._head = 0   # next slot to read
        self._tail = 0   # next slot to write
        self._size = 0
        self._lock = threading.Lock()

        self.accepted = 0
        self.dropped_buffer_full = 0
        self.peak_size = 0

    # -- producer side (request path) ---------------------------------------

    def put(self, span: "Span") -> bool:
        """Offer a span. Returns False if it was dropped.

        Callers ignore the return value; it exists for tests and for the
        exporter's own accounting. Nothing about a dropped span is worth
        raising over or logging per-occurrence — logging on every drop is how
        an overloaded process gets more overloaded.
        """
        with self._lock:
            if self._size >= self._capacity:
                self.dropped_buffer_full += 1
                return False
            self._slots[self._tail] = span
            self._tail = (self._tail + 1) % self._capacity
            self._size += 1
            self.accepted += 1
            if self._size > self.peak_size:
                self.peak_size = self._size
            return True

    # -- consumer side (exporter thread) ------------------------------------

    def drain(self, max_items: int) -> list["Span"]:
        """Remove and return up to `max_items` spans, oldest first."""
        if max_items <= 0:
            return []
        with self._lock:
            count = min(max_items, self._size)
            if count == 0:
                return []
            out: list[Span] = []
            head = self._head
            slots = self._slots
            for _ in range(count):
                out.append(slots[head])
                # Release the reference so the span can be collected as soon
                # as the exporter is done with it.
                slots[head] = None
                head = (head + 1) % self._capacity
            self._head = head
            self._size -= count
            return out

    def drain_all(self) -> list["Span"]:
        return self.drain(self._capacity)

    # -- introspection ------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return self._size

    @property
    def capacity(self) -> int:
        return self._capacity

    def is_full(self) -> bool:
        with self._lock:
            return self._size >= self._capacity

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "buffer_capacity": self._capacity,
                "buffer_size": self._size,
                "buffer_peak_size": self.peak_size,
                "spans_buffered": self.accepted,
                "spans_dropped_buffer_full": self.dropped_buffer_full,
            }

    def reset(self) -> None:
        """Empty the ring and zero the counters. Tests only."""
        with self._lock:
            self._slots = [None] * self._capacity
            self._head = 0
            self._tail = 0
            self._size = 0
            self.accepted = 0
            self.dropped_buffer_full = 0
            self.peak_size = 0
