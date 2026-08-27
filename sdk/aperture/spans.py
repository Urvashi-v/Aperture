"""The span model and the tracer that produces it.

`Span` carries exactly the columns DESIGN.md 5.2 stores in ClickHouse, plus the
OTLP identity fields, because the analysis engine reads those columns and
nothing else. Everything that is not one of those is an attribute.

Two performance decisions worth naming:

* `slots=True`. A slotted instance has no `__dict__`, which is both smaller and
  faster to construct than a plain object. This is the cheap, safe half of the
  "pre-allocate span structs" rule in DESIGN.md 7.1.1.

* No object pool, yet. Recycling span objects would save an allocation and
  introduce a class of use-after-free bugs that are miserable to debug. The
  Week 1 Day 7 milestone is *measure the overhead*; if allocation shows up in
  that measurement, a pool can be added behind this same interface. Optimising
  before the measurement that exists specifically to guide the optimisation
  would be the wrong order.
"""

from __future__ import annotations

import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterator

from aperture.buffer import SpanBuffer
from aperture.config import ApertureConfig
from aperture.context import (
    SpanContext,
    TraceState,
    generate_span_id,
    generate_trace_id,
    get_trace_state,
    new_child_context,
    reset_span_context,
    set_current_span_context,
)

# Unknown row count. Not 0, because "this query returned no rows" and "we could
# not find out how many rows this query returned" are different facts and the
# N+1 detector's row-count ceiling depends on telling them apart.
UNKNOWN_ROWS = -1


# ---------------------------------------------------------------------------
# Clock
#
# Span start times are derived from the monotonic counter against a single
# wall-clock anchor, rather than from `time.time_ns()` per span.
#
# This is not micro-optimisation, it is a correctness fix. On Windows
# `time.time_ns()` has a resolution of 15.625 ms - measured on the development
# machine, 20,000 consecutive calls returned two distinct values. Detector D1's
# non-overlap guard and detector D4's entire premise are statements about
# whether spans overlap in time; at 15 ms granularity, twenty sibling queries
# that each take 2 ms all carry the same timestamp and become indistinguishable
# from a concurrent fan-out. That single confusion would turn the flagship N+1
# detector into a coin flip.
#
# `perf_counter_ns` has 100 ns resolution. Anchoring to it costs one clock read
# per span instead of two, and gives exact ordering within a process.
#
# The tradeoff, stated plainly: absolute timestamps drift away from the system
# clock at whatever rate the two oscillators differ, so a span's wall-clock time
# is approximate over a long-lived process, while *relative* ordering and
# durations inside a trace are exact. Trace analysis only ever asks the second
# kind of question. `resync_clock()` re-anchors if a caller needs it.
# ---------------------------------------------------------------------------

_anchor_wall_ns = time.time_ns()
_anchor_perf_ns = time.perf_counter_ns()


def resync_clock() -> None:
    """Re-anchor wall-clock time to the monotonic counter.

    Can move subsequent timestamps backwards relative to earlier ones, so it
    must not be called while spans are open.
    """
    global _anchor_wall_ns, _anchor_perf_ns
    _anchor_wall_ns = time.time_ns()
    _anchor_perf_ns = time.perf_counter_ns()


def clock_anchor() -> tuple[int, int]:
    """(wall_ns, perf_ns) of the current anchor. For tests."""
    return _anchor_wall_ns, _anchor_perf_ns


class SpanKind(IntEnum):
    """OTel span kinds, with the same numeric values OTLP uses."""

    UNSPECIFIED = 0
    INTERNAL = 1
    SERVER = 2
    CLIENT = 3
    PRODUCER = 4
    CONSUMER = 5


class SpanStatus(IntEnum):
    """OTel status codes, with the same numeric values OTLP uses."""

    UNSET = 0
    OK = 1
    ERROR = 2


# What kind of client a CLIENT span is. The N+1 detector groups DB spans by SQL
# fingerprint and HTTP spans by URL template; it is the same algorithm over two
# populations, so it needs to tell them apart (DESIGN.md 6.2).
CLIENT_KIND_DB = "db"
CLIENT_KIND_HTTP = "http"


@dataclass(slots=True)
class Span:
    """One unit of work. Field order matches DESIGN.md 5.2 where they overlap."""

    trace_id: int
    span_id: int
    parent_span_id: int
    service: str
    operation: str
    kind: SpanKind
    start_unix_ns: int

    duration_ns: int = 0
    status: SpanStatus = SpanStatus.UNSET
    status_message: str = ""
    endpoint: str = ""

    # ---- Database ---------------------------------------------------------
    db_statement: str = ""
    # PLACEHOLDER. This is a hash of the whitespace-normalised statement text,
    # which is enough to group repeated executions of the *same* prepared
    # statement and therefore enough to drive the code-location cache. It is
    # NOT the fingerprint DESIGN.md 6.1 specifies: that one parses to an AST
    # with sqlglot and collapses literals, so that `WHERE id = 42` and
    # `WHERE id = 77` land on one identity. Week 2 Day 9 replaces this.
    # `db_fingerprint_method` records which of the two produced the value, so
    # nothing downstream can mistake one for the other.
    db_fingerprint: int = 0
    db_fingerprint_method: str = ""
    db_rows: int = UNKNOWN_ROWS

    # ---- Connection pool ---------------------------------------------------
    # Time spent waiting for a connection, measured separately from execution
    # time. Detector D3 is the reason this is its own column rather than an
    # attribute: "the pool is saturated" and "the database is slow" look
    # identical until these two numbers are separated (DESIGN.md 6.4).
    pool_wait_ns: int = 0

    # ---- Provenance --------------------------------------------------------
    code_location: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    # Monotonic reading taken at start. Wall-clock is not monotonic and an NTP
    # step mid-request would otherwise produce a negative duration.
    start_perf_ns: int = 0

    @property
    def end_unix_ns(self) -> int:
        return self.start_unix_ns + self.duration_ns

    @property
    def is_db(self) -> bool:
        return self.attributes.get("aperture.client_kind") == CLIENT_KIND_DB

    @property
    def is_http_client(self) -> bool:
        return self.attributes.get("aperture.client_kind") == CLIENT_KIND_HTTP


class Tracer:
    """Creates spans and hands finished ones to the buffer.

    Every method here runs on the request path, so every method here is
    written to be cheap and to never raise. A tracer that can throw is a
    tracer that can take down the application it is measuring.
    """

    __slots__ = (
        "config",
        "buffer",
        "spans_started",
        "spans_finished",
        "spans_over_budget",
        "spans_unsampled",
    )

    def __init__(self, config: ApertureConfig, buffer: SpanBuffer) -> None:
        self.config = config
        self.buffer = buffer
        self.spans_started = 0
        self.spans_finished = 0
        self.spans_over_budget = 0
        self.spans_unsampled = 0

    # -- lifecycle ----------------------------------------------------------

    def start_span(
        self,
        operation: str,
        kind: SpanKind = SpanKind.INTERNAL,
        *,
        parent: SpanContext | None = None,
        attributes: dict[str, str] | None = None,
    ) -> Span | None:
        """Open a span, or return None if it should not be recorded.

        Returning None rather than a no-op span is deliberate: callers check
        for it once, and a dropped span then costs nothing further. Every hook
        in this package treats None as "carry on without instrumentation".
        """
        state = get_trace_state()
        if state is not None:
            if state.span_count >= self.config.max_spans_per_trace:
                state.over_budget += 1
                self.spans_over_budget += 1
                return None
            state.span_count += 1

        ctx = new_child_context(parent)
        if not ctx.sampled:
            self.spans_unsampled += 1
            return None

        perf_ns = time.perf_counter_ns()
        span = Span(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            service=self.config.service_name,
            operation=operation,
            kind=kind,
            start_unix_ns=_anchor_wall_ns + (perf_ns - _anchor_perf_ns),
            start_perf_ns=perf_ns,
            # resolve_endpoint() discovers the templated route the first time
            # a span is created after the framework has dispatched, then
            # caches it for the rest of the request.
            endpoint=state.resolve_endpoint() if state is not None else "",
        )
        if attributes:
            for key, value in attributes.items():
                self.set_attribute(span, key, value)

        self.spans_started += 1
        return span

    def start_root_span(
        self,
        operation: str,
        kind: SpanKind = SpanKind.SERVER,
        *,
        remote_parent: SpanContext | None = None,
        endpoint: str = "",
    ) -> tuple[Span | None, TraceState]:
        """Begin a trace, honouring the head sample rate.

        This is the only place head sampling is applied. Child spans inherit
        the decision through `SpanContext.sampled`, so a trace is never half
        recorded — a partial trace is worse than no trace, because the
        structural detectors reason about a *complete* tree.
        """
        state = TraceState(endpoint=endpoint)

        sampled = True
        rate = self.config.head_sample_rate
        if rate < 1.0 and (rate <= 0.0 or random.random() >= rate):
            sampled = False

        if remote_parent is not None:
            # An upstream service already made the sampling decision; honour it.
            ctx = remote_parent._replace(sampled=remote_parent.sampled and sampled)
        else:
            ctx = SpanContext(
                trace_id=generate_trace_id(),
                span_id=generate_span_id(),
                parent_span_id=0,
                sampled=sampled,
                remote=False,
            )

        if not ctx.sampled:
            self.spans_unsampled += 1
            return None, state

        state.span_count = 1
        perf_ns = time.perf_counter_ns()
        span = Span(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            service=self.config.service_name,
            operation=operation,
            kind=kind,
            start_unix_ns=_anchor_wall_ns + (perf_ns - _anchor_perf_ns),
            start_perf_ns=perf_ns,
            endpoint=endpoint,
        )
        self.spans_started += 1
        return span, state

    def end_span(
        self,
        span: Span | None,
        *,
        status: SpanStatus | None = None,
        message: str = "",
    ) -> None:
        """Close a span and offer it to the buffer.

        This does not send anything. It appends to an in-memory ring and
        returns — the request path never performs network I/O (DESIGN.md
        7.1.2). If the ring is full the span is dropped and counted.
        """
        if span is None:
            return
        try:
            span.duration_ns = time.perf_counter_ns() - span.start_perf_ns
            if status is not None:
                span.status = status
            if message:
                span.status_message = message[: self.config.max_attribute_chars]
            if not span.endpoint:
                state = get_trace_state()
                if state is not None:
                    span.endpoint = state.resolve_endpoint()
            self.spans_finished += 1
            self.buffer.put(span)
        except Exception:  # pragma: no cover - defensive; must never propagate
            pass

    # -- attributes ---------------------------------------------------------

    def set_attribute(self, span: Span | None, key: str, value: object) -> None:
        """Record an attribute, clamped to the configured limits.

        Attributes are stored as strings because the ClickHouse column is
        `Map(String, String)`. Converting here rather than at export keeps the
        exporter free of type dispatch.
        """
        if span is None:
            return
        attributes = span.attributes
        if len(attributes) >= self.config.max_attributes_per_span and key not in attributes:
            return
        try:
            text = value if isinstance(value, str) else str(value)
        except Exception:  # pragma: no cover - a __str__ that raises
            return
        attributes[key] = text[: self.config.max_attribute_chars]

    # -- convenience --------------------------------------------------------

    @contextmanager
    def span(
        self,
        operation: str,
        kind: SpanKind = SpanKind.INTERNAL,
        *,
        attributes: dict[str, str] | None = None,
    ) -> Iterator[Span | None]:
        """Open a span, make it current for the block, and close it."""
        span = self.start_span(operation, kind, attributes=attributes)
        if span is None:
            yield None
            return

        token = set_current_span_context(
            SpanContext(
                trace_id=span.trace_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                sampled=True,
                remote=False,
            )
        )
        try:
            yield span
        except BaseException as exc:
            self.end_span(span, status=SpanStatus.ERROR, message=repr(exc))
            raise
        else:
            if span.status is SpanStatus.UNSET:
                span.status = SpanStatus.OK
            self.end_span(span)
        finally:
            reset_span_context(token)

    def stats(self) -> dict[str, int]:
        return {
            "spans_started": self.spans_started,
            "spans_finished": self.spans_finished,
            "spans_dropped_over_budget": self.spans_over_budget,
            "spans_dropped_unsampled": self.spans_unsampled,
        }


# ---------------------------------------------------------------------------
# Process-wide tracer.
#
# The hooks live in submodules and are installed as library-level callbacks, so
# they cannot be handed a tracer by an argument. They read it from here.
# `None` means "not instrumented", and every hook checks for it first — which
# is also what makes the whole SDK a no-op when it is disabled.
# ---------------------------------------------------------------------------

_tracer: Tracer | None = None


def get_tracer() -> Tracer | None:
    return _tracer


def set_tracer(tracer: Tracer | None) -> None:
    global _tracer
    _tracer = tracer
