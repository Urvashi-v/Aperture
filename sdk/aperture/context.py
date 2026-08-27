"""Trace context and its propagation across `await` boundaries.

Two things live here:

1. **In-process propagation** via `contextvars`. This is the mechanism that
   makes a DB query issued three `await`s deep know which HTTP request it
   belongs to, without the application passing anything down. It also survives
   `asyncio.gather`, because each task inherits a copy of the context at
   creation.

2. **Cross-process propagation** via the W3C `traceparent` header, which is the
   format OpenTelemetry uses on the wire. Accepting it on the way in and
   emitting it on the way out is what will let a trace span two services when
   the chatty-microservice pathology (P8) arrives.

Identifiers are held as **integers**, not bytes or hex strings. Generating an
int is one `random.getrandbits` call; the conversion to the 16- and 8-byte
big-endian fields OTLP wants happens in the exporter, on the background thread,
where it costs the request path nothing.
"""

from __future__ import annotations

import random
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

# W3C trace-flags bit 0: this trace is sampled.
FLAG_SAMPLED = 0x01

INVALID_TRACE_ID = 0
INVALID_SPAN_ID = 0

_TRACEPARENT_HEADER = "traceparent"

# OTel's Python implementation uses the module-level Mersenne Twister for the
# same purpose. Trace ids need to be unique, not unguessable, and a CSPRNG call
# per span would be a real cost on the hot path for no benefit.
_rand = random.getrandbits


class SpanContext(NamedTuple):
    """Identity of one span, and the trace it belongs to."""

    trace_id: int          # 128-bit
    span_id: int           # 64-bit
    parent_span_id: int    # 64-bit; 0 means this span is the trace root
    sampled: bool
    # True when this context was parsed from an inbound header rather than
    # created here. The collector needs to know which service owns the root.
    remote: bool = False

    @property
    def is_valid(self) -> bool:
        return self.trace_id != INVALID_TRACE_ID and self.span_id != INVALID_SPAN_ID

    def hex_trace_id(self) -> str:
        return format(self.trace_id, "032x")

    def hex_span_id(self) -> str:
        return format(self.span_id, "016x")


@dataclass(slots=True)
class TraceState:
    """Request-scoped state shared by every span in one trace.

    Stored once in a `ContextVar` and then *mutated in place*. That is
    deliberate: a mutable object reached through a context variable is visible
    to every coroutine in the task tree, including tasks spawned by
    `asyncio.gather`, so the span budget below is genuinely per-request rather
    than per-coroutine.
    """

    endpoint: str = ""
    span_count: int = 0
    # Spans refused because the trace hit `max_spans_per_trace`. A single
    # runaway loop must not be able to produce an unbounded trace (C3).
    over_budget: int = 0
    # Total time this request spent waiting for a database connection, summed
    # across every checkout. Detector D3 compares this against execution time
    # to tell pool saturation apart from a slow database (DESIGN.md 6.4).
    pool_wait_ns: int = 0
    attributes: dict[str, str] = field(default_factory=dict)

    # Set by the ASGI middleware. The templated route (`/api/products/{id}`)
    # is not known when the request span opens - the framework resolves it
    # during dispatch - but it *is* known by the time the handler runs its
    # first query. This callable lets the first span created after dispatch
    # discover it, after which the answer is cached in `endpoint` and the
    # resolver is dropped. Kept as a plain callable so nothing in the core
    # depends on ASGI.
    endpoint_resolver: Callable[[], str] | None = None

    def resolve_endpoint(self) -> str:
        """Return the templated route, resolving it once if it is available."""
        if self.endpoint:
            return self.endpoint
        resolver = self.endpoint_resolver
        if resolver is None:
            return ""
        try:
            value = resolver()
        except Exception:
            value = ""
        if value:
            self.endpoint = value
            self.endpoint_resolver = None
        return self.endpoint


_span_context: ContextVar[SpanContext | None] = ContextVar(
    "aperture_span_context", default=None
)
_trace_state: ContextVar[TraceState | None] = ContextVar(
    "aperture_trace_state", default=None
)


# ---------------------------------------------------------------------------
# Identifier generation
# ---------------------------------------------------------------------------


def generate_trace_id() -> int:
    """A random 128-bit trace id, never zero."""
    while True:
        value = _rand(128)
        if value != INVALID_TRACE_ID:
            return value


def generate_span_id() -> int:
    """A random 64-bit span id, never zero."""
    while True:
        value = _rand(64)
        if value != INVALID_SPAN_ID:
            return value


# ---------------------------------------------------------------------------
# In-process propagation
# ---------------------------------------------------------------------------


def get_current_span_context() -> SpanContext | None:
    return _span_context.get()


def set_current_span_context(ctx: SpanContext | None) -> Token:
    return _span_context.set(ctx)


def reset_span_context(token: Token) -> None:
    """Restore the previous context.

    Tolerates a token created in a different context, which happens when a
    span is opened in one task and closed in another. Losing the restore is
    strictly better than raising inside a `finally` in someone's request path.
    """
    try:
        _span_context.reset(token)
    except ValueError:
        _span_context.set(None)


def get_trace_state() -> TraceState | None:
    return _trace_state.get()


def set_trace_state(state: TraceState | None) -> Token:
    return _trace_state.set(state)


def reset_trace_state(token: Token) -> None:
    try:
        _trace_state.reset(token)
    except ValueError:
        _trace_state.set(None)


def current_endpoint() -> str:
    state = _trace_state.get()
    return state.endpoint if state is not None else ""


def new_child_context(parent: SpanContext | None = None) -> SpanContext:
    """Derive a child of `parent`, or start a fresh trace when there is none."""
    if parent is None:
        parent = _span_context.get()

    if parent is None or not parent.is_valid:
        return SpanContext(
            trace_id=generate_trace_id(),
            span_id=generate_span_id(),
            parent_span_id=INVALID_SPAN_ID,
            sampled=True,
            remote=False,
        )

    return SpanContext(
        trace_id=parent.trace_id,
        span_id=generate_span_id(),
        parent_span_id=parent.span_id,
        sampled=parent.sampled,
        remote=False,
    )


# ---------------------------------------------------------------------------
# W3C Trace Context (https://www.w3.org/TR/trace-context/)
# ---------------------------------------------------------------------------


def format_traceparent(ctx: SpanContext) -> str:
    """Render a context as a `traceparent` header value."""
    flags = FLAG_SAMPLED if ctx.sampled else 0x00
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{flags:02x}"


def parse_traceparent(header: str | None) -> SpanContext | None:
    """Parse an inbound `traceparent`, or return None if it is unusable.

    Never raises. An upstream service sending a malformed header is a reason
    to start a new trace, not a reason to fail the request.
    """
    if not header:
        return None

    parts = header.strip().split("-")
    if len(parts) < 4:
        return None

    version, trace_hex, span_hex, flags_hex = parts[0], parts[1], parts[2], parts[3]

    # Version ff is forbidden by the spec. Future versions are permitted to
    # append fields, so anything above 00 is accepted on a best-effort basis.
    if len(version) != 2 or version == "ff":
        return None
    if len(trace_hex) != 32 or len(span_hex) != 16 or len(flags_hex) != 2:
        return None

    try:
        trace_id = int(trace_hex, 16)
        span_id = int(span_hex, 16)
        flags = int(flags_hex, 16)
    except ValueError:
        return None

    if trace_id == INVALID_TRACE_ID or span_id == INVALID_SPAN_ID:
        return None

    return SpanContext(
        trace_id=trace_id,
        # The inbound span becomes our parent; this process gets a fresh id.
        span_id=generate_span_id(),
        parent_span_id=span_id,
        sampled=bool(flags & FLAG_SAMPLED),
        remote=True,
    )


def extract_from_headers(headers: dict[str, str]) -> SpanContext | None:
    """Pull a context out of a lowercase-keyed header mapping."""
    return parse_traceparent(headers.get(_TRACEPARENT_HEADER))


def inject_into_headers(headers: dict[str, str], ctx: SpanContext) -> dict[str, str]:
    """Add `traceparent` to an outbound header mapping, in place."""
    headers[_TRACEPARENT_HEADER] = format_traceparent(ctx)
    return headers
