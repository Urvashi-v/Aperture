"""Aperture instrumentation SDK.

Typical integration — this is the whole thing:

    from aperture import instrument_app

    app = FastAPI()
    instrument_app(app, service_name="sample-shop", service_version="1.4.2")

That single call adds one ASGI middleware and installs class-level hooks for
SQLAlchemy, the connection pool and httpx. The application does not pass in its
engine, does not wrap its sessions, and does not touch its request handlers
(design constraint C2).

Nothing happens unless `APERTURE_SDK_ENABLED=true`. Instrumentation that turns
itself on because a package happens to be installed is how a dependency bump
becomes an incident.

Three properties this SDK will not trade away:

* **The request path never does network I/O.** Finished spans go into a fixed
  ring buffer; a daemon thread exports them.
* **Memory is bounded.** The ring, the code-location cache and the per-trace
  span budget all have ceilings. Nothing here grows with traffic.
* **It fails open.** A collector that is down, a malformed config value, a bug
  in a hook — the application continues, spans are dropped, and counters go up.
"""

from __future__ import annotations

import atexit
import logging
from typing import Any

from aperture.buffer import SpanBuffer
from aperture.config import ApertureConfig
from aperture.context import (
    SpanContext,
    TraceState,
    format_traceparent,
    get_current_span_context,
    get_trace_state,
    parse_traceparent,
)
from aperture.exporter import OtlpSpanExporter
from aperture.middleware import ApertureMiddleware
from aperture.spans import (
    CLIENT_KIND_DB,
    CLIENT_KIND_HTTP,
    UNKNOWN_ROWS,
    Span,
    SpanKind,
    SpanStatus,
    Tracer,
    get_tracer,
    set_tracer,
)

__version__ = "0.1.0"

__all__ = [
    "ApertureConfig",
    "ApertureMiddleware",
    "Span",
    "SpanContext",
    "SpanKind",
    "SpanStatus",
    "TraceState",
    "Tracer",
    "CLIENT_KIND_DB",
    "CLIENT_KIND_HTTP",
    "UNKNOWN_ROWS",
    "drain_buffered_spans",
    "flush",
    "format_traceparent",
    "get_current_span_context",
    "get_stats",
    "get_trace_state",
    "get_tracer",
    "instrument",
    "instrument_app",
    "is_enabled",
    "parse_traceparent",
    "shutdown",
]

logger = logging.getLogger("aperture")

_config: ApertureConfig | None = None
_buffer: SpanBuffer | None = None
_exporter: OtlpSpanExporter | None = None
_installed_hooks: list[str] = []
_atexit_registered = False


def is_enabled() -> bool:
    return get_tracer() is not None


def instrument(
    config: ApertureConfig | None = None, **overrides: Any
) -> ApertureConfig:
    """Install hooks and start the exporter. Idempotent.

    Returns the effective configuration whether or not instrumentation was
    actually enabled, so a caller can log what it got.
    """
    global _config, _buffer, _exporter, _installed_hooks, _atexit_registered

    # Checked first, and before the environment is consulted at all. If
    # instrumentation is already running, the honest answer to "what is the
    # configuration" is the live one — not whatever the environment says now.
    # Re-reading it here would make `instrument_app()` report the SDK as
    # disabled in a process that had been instrumented by an explicit
    # `instrument(config)` call, and then decline to add the middleware.
    if get_tracer() is not None:
        # Re-configuring a live tracer mid-flight would give some spans one
        # service name and some another; a caller who wants new settings
        # should shutdown() first.
        logger.debug("aperture: already instrumented, ignoring repeat call")
        return _config if _config is not None else ApertureConfig()

    effective = config or ApertureConfig.from_env(**overrides)

    if not effective.enabled:
        logger.info(
            "aperture: disabled (set APERTURE_SDK_ENABLED=true to enable)"
        )
        _config = effective
        return effective

    from aperture.hooks import install_all

    _config = effective
    _buffer = SpanBuffer(effective.buffer_capacity)
    _exporter = OtlpSpanExporter(effective, _buffer)
    _exporter.start()
    set_tracer(Tracer(effective, _buffer))

    _installed_hooks = install_all(effective)

    if not _atexit_registered:
        atexit.register(_atexit_shutdown)
        _atexit_registered = True

    logger.info(
        "aperture: instrumentation active (service=%s endpoint=%s hooks=%s "
        "buffer=%d stats_path=%s)",
        effective.service_name,
        effective.collector_endpoint,
        ",".join(_installed_hooks) or "none",
        effective.buffer_capacity,
        effective.stats_path or "disabled",
    )
    return effective


def instrument_app(app: Any, **overrides: Any) -> Any:
    """Instrument an ASGI application.

    Adds the middleware via `app.add_middleware` when the framework offers it
    (Starlette and FastAPI do), and otherwise returns a wrapped application, so
    this works for a bare ASGI callable too.

    When the SDK is disabled the application is returned untouched and no
    middleware is added at all — a disabled SDK costs the request path
    literally nothing, not even one extra `await`.
    """
    effective = instrument(**overrides)
    if not effective.enabled:
        return app

    add_middleware = getattr(app, "add_middleware", None)
    if callable(add_middleware):
        add_middleware(ApertureMiddleware, config=effective)
        return app
    return ApertureMiddleware(app, config=effective)


def flush(timeout: float = 2.0) -> bool:
    """Block until buffered spans have been exported, or the timeout expires.

    For tests, batch jobs and shutdown. Never call this from a request
    handler: it waits on network I/O.
    """
    if _exporter is None:
        return True
    return _exporter.flush(timeout)


def drain_buffered_spans() -> list[Span]:
    """Remove and return every span currently in the buffer.

    For tests and interactive debugging. The spans come *out* of the buffer,
    so anything drained this way is not exported. There is no supported reason
    to call this from application code.
    """
    if _buffer is None:
        return []
    return _buffer.drain_all()


def shutdown(timeout: float | None = None) -> bool:
    """Remove instrumentation and drain the buffer. Returns True if it drained."""
    global _buffer, _exporter, _installed_hooks

    from aperture.hooks import uninstall_all

    # Stop producing before draining, so the buffer cannot be refilled while
    # the exporter is trying to empty it.
    set_tracer(None)
    uninstall_all()
    _installed_hooks = []

    drained = True
    if _exporter is not None:
        drained = _exporter.shutdown(timeout)
    _exporter = None
    _buffer = None
    return drained


def _atexit_shutdown() -> None:
    try:
        shutdown()
    except Exception:  # pragma: no cover - interpreter teardown
        pass


def get_stats() -> dict[str, Any]:
    """Everything the SDK knows about its own behaviour.

    Deliberately includes the drop counters. "How many spans did you throw
    away, and why" is the question an operator actually needs answered, and a
    telemetry library that cannot answer it is asking to be trusted on faith.
    """
    stats: dict[str, Any] = {
        "version": __version__,
        "enabled": is_enabled(),
        "hooks_installed": list(_installed_hooks),
    }
    if _config is not None:
        stats["config"] = _config.describe()

    tracer = get_tracer()
    if tracer is not None:
        stats.update(tracer.stats())
    if _buffer is not None:
        stats.update(_buffer.stats())
    if _exporter is not None:
        stats.update(_exporter.stats())

    try:
        from aperture.hooks import code_location_cache_size
        from aperture.hooks import pool as pool_hook

        stats["code_location_cache_size"] = code_location_cache_size()
        stats.update(pool_hook.stats())
    except Exception:  # pragma: no cover - stats must never raise
        pass

    return stats
