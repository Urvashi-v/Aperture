"""SQLAlchemy query instrumentation.

Listens on the `Engine` *class*, not on an engine instance. That is what makes
constraint C2 achievable: the application never has to hand us its engine, and
an engine created later - by a test fixture, a migration, a second database -
is instrumented automatically.

Produces one CLIENT span per statement execution, carrying the statement text,
a placeholder fingerprint, the row count, the call site, and a digest of the
bound parameters. Those five things are precisely what detector D1 needs to
decide whether a group of sibling queries is an N+1 (DESIGN.md 6.2).

**Bound parameters are hashed, never stored.** The N+1 definition requires
distinguishing "the same query with varying parameters" (an N+1) from "the same
query with identical parameters" (a caching problem), which needs parameter
*variance* and not parameter *values*. A digest gives the first without ever
putting customer data in a telemetry pipeline.
"""

from __future__ import annotations

import hashlib
from typing import Any

from aperture.config import ApertureConfig
from aperture.context import get_trace_state
from aperture.hooks import (
    FINGERPRINT_MASK,
    FINGERPRINT_METHOD_PLACEHOLDER,
    cached_code_location,
    placeholder_fingerprint,
    safe,
)
from aperture.spans import CLIENT_KIND_DB, SpanKind, SpanStatus, get_tracer

_INFO_SPAN_STACK = "_aperture_db_spans"
_INFO_POOL_WAIT = "_aperture_pool_wait_ns"

_installed = False
_config: ApertureConfig | None = None

# Statements SQLAlchemy issues on its own behalf. Instrumenting them adds noise
# to every trace and, worse, would make the connection-setup queries look like
# an N+1 on a cold pool.
_INTERNAL_STATEMENT_PREFIXES = (
    "select pg_catalog",
    "show ",
    "set ",
    "begin",
    "commit",
    "rollback",
)


def _params_digest(parameters: Any, limit: int = 512) -> int:
    """A 64-bit digest of the bound parameters. Values never leave this call."""
    try:
        if isinstance(parameters, (list, tuple)) and len(parameters) > 8:
            # executemany: sample the head rather than rendering thousands of
            # rows just to throw the string away.
            sample = repr(parameters[:8])
        else:
            sample = repr(parameters)
    except Exception:
        return 0
    digest = hashlib.blake2b(
        sample[:limit].encode("utf-8", "replace"), digest_size=8
    )
    # Masked for the same reason as the fingerprint: OTLP int attributes are
    # signed int64 and a full 64-bit hash overflows them.
    return int.from_bytes(digest.digest(), "big") & FINGERPRINT_MASK


def _operation_name(statement: str) -> tuple[str, str]:
    """Span name and `db.operation`, from the leading keyword.

    Deliberately not a parser. A readable span name only needs the verb, and
    anything that looks like SQL parsing here would be the regex mistake
    DESIGN.md 6.1 warns about wearing a different hat. Table extraction waits
    for sqlglot in Week 2.
    """
    head = statement.lstrip()[:16].split(None, 1)
    verb = head[0].upper() if head and head[0].isalpha() else "SQL"
    return f"db.{verb.lower()}", verb


@safe
def _before_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    tracer = get_tracer()
    config = _config
    if tracer is None or config is None:
        return

    lowered = statement[:24].lower()
    if lowered.startswith(_INTERNAL_STATEMENT_PREFIXES):
        return

    fingerprint = placeholder_fingerprint(statement)
    operation, verb = _operation_name(statement)

    span = tracer.start_span(operation, SpanKind.CLIENT)
    if span is None:
        return

    span.db_fingerprint = fingerprint
    span.db_fingerprint_method = FINGERPRINT_METHOD_PLACEHOLDER
    if config.capture_db_statement:
        span.db_statement = statement[: config.max_statement_chars]

    location, function = cached_code_location(config, fingerprint)
    if location:
        span.code_location = location
        tracer.set_attribute(span, "code.function", function)

    tracer.set_attribute(span, "aperture.client_kind", CLIENT_KIND_DB)
    tracer.set_attribute(span, "db.operation", verb)
    tracer.set_attribute(span, "aperture.db.params_digest", _params_digest(parameters))
    try:
        tracer.set_attribute(span, "db.system", conn.dialect.name)
    except Exception:
        pass
    if executemany:
        tracer.set_attribute(span, "aperture.db.executemany", "true")

    # Claim any pool wait recorded for this checkout. Consumed exactly once so
    # that summing pool_wait_ns across spans gives the real total rather than
    # the wait multiplied by the number of queries on the connection.
    info = conn.info
    wait_ns = info.pop(_INFO_POOL_WAIT, 0)
    if wait_ns:
        span.pool_wait_ns = wait_ns
        state = get_trace_state()
        if state is not None:
            state.pool_wait_ns += wait_ns

    # A stack rather than a slot: executions on one connection are serial, but
    # a nested execution inside an event handler would otherwise lose a span.
    stack = info.get(_INFO_SPAN_STACK)
    if stack is None:
        stack = []
        info[_INFO_SPAN_STACK] = stack
    stack.append(span)


@safe
def _after_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    tracer = get_tracer()
    if tracer is None:
        return
    stack = conn.info.get(_INFO_SPAN_STACK)
    if not stack:
        return
    span = stack.pop()

    # rowcount is -1 when the driver cannot say. Leaving db_rows at
    # UNKNOWN_ROWS in that case matters: the N+1 row-count ceiling must not
    # read "unknown" as "zero rows".
    try:
        rowcount = cursor.rowcount
        if rowcount is not None and rowcount >= 0:
            span.db_rows = int(rowcount)
    except Exception:
        pass

    tracer.end_span(span, status=SpanStatus.OK)


@safe
def _handle_error(exception_context: Any) -> None:
    """Close the span for a statement that raised.

    `after_cursor_execute` does not fire on failure, so without this a failed
    query would leak its span and the trace would be missing the very
    operation that broke the request.
    """
    tracer = get_tracer()
    if tracer is None:
        return
    conn = getattr(exception_context, "connection", None)
    if conn is None:
        return
    stack = conn.info.get(_INFO_SPAN_STACK)
    if not stack:
        return
    span = stack.pop()
    tracer.end_span(
        span,
        status=SpanStatus.ERROR,
        message=repr(getattr(exception_context, "original_exception", "")),
    )


def install(config: ApertureConfig) -> bool:
    global _installed, _config
    _config = config
    if _installed:
        return True
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except ImportError:
        return False

    event.listen(Engine, "before_cursor_execute", _before_cursor_execute)
    event.listen(Engine, "after_cursor_execute", _after_cursor_execute)
    event.listen(Engine, "handle_error", _handle_error)
    _installed = True
    return True


def uninstall() -> None:
    global _installed, _config
    if not _installed:
        return
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        event.remove(Engine, "before_cursor_execute", _before_cursor_execute)
        event.remove(Engine, "after_cursor_execute", _after_cursor_execute)
        event.remove(Engine, "handle_error", _handle_error)
    except Exception:
        pass
    _installed = False
    _config = None


def is_installed() -> bool:
    return _installed
