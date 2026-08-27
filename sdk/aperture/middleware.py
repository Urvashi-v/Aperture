"""ASGI middleware — the only thing an application has to add.

This is design constraint C2 made concrete: one middleware registration, and
everything else (queries, pool waits, outbound HTTP) is picked up by
class-level hooks that need no further cooperation from the application.

Written as raw ASGI rather than Starlette's `BaseHTTPMiddleware`, for two
reasons that both matter here:

* `BaseHTTPMiddleware` runs the downstream application in a *separate*
  `anyio` task. Context variables set before that boundary are copied into the
  child task, but mutations made inside it do not propagate back - which would
  break the span budget and the pool-wait accumulator, both of which are
  mutated deep inside the request and read at the end.
* It also buffers the response body through a memory stream, which adds real
  latency to a streaming response. An instrumentation library has no business
  changing the shape of somebody's responses.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, MutableMapping

from aperture.config import ApertureConfig
from aperture.context import (
    SpanContext,
    parse_traceparent,
    reset_span_context,
    reset_trace_state,
    set_current_span_context,
    set_trace_state,
)
from aperture.spans import SpanKind, SpanStatus, get_tracer

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

_TRACEPARENT = b"traceparent"


def _extract_parent(scope: Scope) -> SpanContext | None:
    """Find and parse an inbound `traceparent`.

    Scans the raw header list instead of building a dict. Requests carry a
    dozen or more headers and we want exactly one of them; materialising a
    dictionary per request to answer that would be wasted allocation on the
    hot path.
    """
    try:
        for key, value in scope.get("headers", ()):
            if key == _TRACEPARENT:
                return parse_traceparent(value.decode("latin-1"))
    except Exception:
        pass
    return None


def _route_template(scope: Scope) -> str:
    """The templated path (`/api/products/{product_id}`), once dispatch has run.

    Starlette puts the matched `Route` into the scope during dispatch, so this
    returns "" before the router has run and the real template afterwards.
    Grouping by this instead of the concrete path is what keeps endpoint
    cardinality at a few dozen values rather than one per product id.
    """
    route = scope.get("route")
    if route is None:
        return ""
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else ""


class ApertureMiddleware:
    """Wraps an ASGI application in a SERVER span."""

    def __init__(self, app: Any, config: ApertureConfig | None = None) -> None:
        self.app = app
        self._config = config

    @property
    def config(self) -> ApertureConfig | None:
        if self._config is not None:
            return self._config
        tracer = get_tracer()
        return tracer.config if tracer is not None else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan and websocket scopes pass straight through. So does every
        # request when the SDK is disabled, which is what makes the disabled
        # state genuinely free rather than merely cheap.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        tracer = get_tracer()
        if tracer is None:
            await self.app(scope, receive, send)
            return

        config = self.config
        if config is not None and config.stats_path and scope.get("path") == config.stats_path:
            await self._serve_stats(send)
            return

        method = scope.get("method", "GET")
        raw_path = scope.get("path", "")

        span, state = tracer.start_root_span(
            f"{method} {raw_path}",
            SpanKind.SERVER,
            remote_parent=_extract_parent(scope),
        )
        # Child spans discover the templated route through this once the
        # framework has dispatched. See TraceState.resolve_endpoint.
        state.endpoint_resolver = lambda: _route_template(scope)

        state_token = set_trace_state(state)
        ctx_token = None
        if span is not None:
            ctx_token = set_current_span_context(
                SpanContext(
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    sampled=True,
                )
            )

        status_code = 500
        response_started = False

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code, response_started
            if message.get("type") == "http.response.start":
                status_code = message.get("status", 500)
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:
            self._finish(
                tracer, span, state, scope, method, raw_path,
                status_code=500,
                status=SpanStatus.ERROR,
                message=repr(exc),
            )
            raise
        else:
            self._finish(
                tracer, span, state, scope, method, raw_path,
                status_code=status_code,
                status=SpanStatus.ERROR if status_code >= 500 else SpanStatus.OK,
                message="" if response_started else "no response started",
            )
        finally:
            if ctx_token is not None:
                reset_span_context(ctx_token)
            reset_trace_state(state_token)

    # -- helpers ------------------------------------------------------------

    def _finish(
        self,
        tracer: Any,
        span: Any,
        state: Any,
        scope: Scope,
        method: str,
        raw_path: str,
        *,
        status_code: int,
        status: SpanStatus,
        message: str,
    ) -> None:
        if span is None:
            return
        try:
            endpoint = state.resolve_endpoint() or raw_path
            span.endpoint = endpoint
            # Renamed now that the route is known, so the operation name is a
            # low-cardinality grouping key rather than one name per id.
            span.operation = f"{method} {endpoint}"

            tracer.set_attribute(span, "http.request.method", method)
            tracer.set_attribute(span, "http.response.status_code", status_code)
            tracer.set_attribute(span, "url.path", raw_path)
            query = scope.get("query_string") or b""
            if query:
                # Presence and size only. The values are not ours to ship.
                tracer.set_attribute(span, "aperture.http.query_bytes", len(query))
            if state.span_count:
                tracer.set_attribute(span, "aperture.trace.span_count", state.span_count)
            if state.over_budget:
                tracer.set_attribute(
                    span, "aperture.trace.spans_over_budget", state.over_budget
                )
            # The request's total connection-acquisition wait, summed across
            # every checkout it made. D3's numerator.
            if state.pool_wait_ns:
                span.pool_wait_ns = state.pool_wait_ns
        except Exception:
            pass
        tracer.end_span(span, status=status, message=message)

    async def _serve_stats(self, send: Send) -> None:
        """Answer the diagnostics path with the SDK's own counters.

        Off unless `APERTURE_STATS_PATH` is set. It exists so that "spans are
        being dropped" is an observable fact from outside the process rather
        than something you have to attach a debugger to discover.
        """
        from aperture import get_stats

        try:
            body = json.dumps(get_stats(), default=str).encode("utf-8")
        except Exception:
            body = b'{"error":"stats unavailable"}'

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
