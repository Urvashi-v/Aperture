"""The ASGI middleware: request completion, routing, propagation, failures."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException

import aperture
from aperture.context import SpanContext, format_traceparent
from aperture.middleware import ApertureMiddleware
from aperture.spans import SpanKind, SpanStatus, get_tracer


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict:
        return {"pong": True}

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict:
        return {"id": item_id}

    @app.get("/nested")
    async def nested() -> dict:
        tracer = get_tracer()
        if tracer is not None:
            with tracer.span("inner-work"):
                pass
        return {"ok": True}

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("handler exploded")

    @app.get("/missing")
    async def missing() -> dict:
        raise HTTPException(status_code=404, detail="nope")

    return app


class Response:
    """Just enough of a response for these assertions."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        import json

        return json.loads(self._body)


async def call(app, path: str, headers: dict | None = None, method: str = "GET") -> Response:
    """Drive the ASGI app directly, with no HTTP client in the way.

    Using httpx here would be a mistake that took a while to spot: the SDK
    instruments `httpx.AsyncClient.send` at class level, so the test client
    would create its own CLIENT span, become the parent of the server span,
    and overwrite any `traceparent` the test was trying to send. Speaking ASGI
    directly tests the middleware at exactly the interface it implements.
    """
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }

    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(scope, receive, send)

    status = next(
        (m["status"] for m in messages if m["type"] == "http.response.start"), 0
    )
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return Response(status, body)


def server_spans(spans) -> list:
    return [s for s in spans if s.kind is SpanKind.SERVER]


# ---------------------------------------------------------------------------
# Request completion
# ---------------------------------------------------------------------------


async def test_a_completed_request_produces_one_server_span(instrumented) -> None:
    instrumented()
    app = ApertureMiddleware(build_app())

    response = await call(app, "/ping")
    assert response.status_code == 200

    spans = server_spans(aperture.drain_buffered_spans())
    assert len(spans) == 1
    span = spans[0]
    assert span.kind is SpanKind.SERVER
    assert span.status is SpanStatus.OK
    assert span.duration_ns > 0
    assert span.parent_span_id == 0
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.response.status_code"] == "200"


async def test_the_span_is_named_and_grouped_by_route_template(instrumented) -> None:
    """Endpoint cardinality has to stay low, or every id becomes its own group."""
    instrumented()
    app = ApertureMiddleware(build_app())

    for item_id in (1, 2, 3):
        await call(app, f"/items/{item_id}")

    spans = server_spans(aperture.drain_buffered_spans())
    assert {s.endpoint for s in spans} == {"/items/{item_id}"}
    assert {s.operation for s in spans} == {"GET /items/{item_id}"}
    assert {s.attributes["url.path"] for s in spans} == {"/items/1", "/items/2", "/items/3"}


async def test_child_spans_nest_under_the_request(instrumented) -> None:
    instrumented()
    app = ApertureMiddleware(build_app())

    await call(app, "/nested")

    spans = aperture.drain_buffered_spans()
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    inner = next(s for s in spans if s.operation == "inner-work")
    assert inner.parent_span_id == server.span_id
    assert inner.trace_id == server.trace_id
    # The child was created after dispatch, so it knows the route too.
    assert inner.endpoint == "/nested"


async def test_span_count_is_recorded_on_the_request(instrumented) -> None:
    instrumented()
    app = ApertureMiddleware(build_app())
    await call(app, "/nested")

    server = server_spans(aperture.drain_buffered_spans())[0]
    assert int(server.attributes["aperture.trace.span_count"]) == 2


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_a_handler_exception_is_recorded_and_still_raised(instrumented) -> None:
    instrumented()
    app = ApertureMiddleware(build_app())

    with pytest.raises(RuntimeError, match="handler exploded"):
        await call(app, "/boom")

    span = server_spans(aperture.drain_buffered_spans())[0]
    assert span.status is SpanStatus.ERROR
    assert "handler exploded" in span.status_message


async def test_a_client_error_is_not_a_server_error(instrumented) -> None:
    """4xx means the caller was wrong. Marking it ERROR would drown the signal."""
    instrumented()
    app = ApertureMiddleware(build_app())

    response = await call(app, "/missing")
    assert response.status_code == 404

    span = server_spans(aperture.drain_buffered_spans())[0]
    assert span.status is SpanStatus.OK
    assert span.attributes["http.response.status_code"] == "404"


# ---------------------------------------------------------------------------
# Context propagation across the process boundary
# ---------------------------------------------------------------------------


async def test_an_inbound_traceparent_continues_the_trace(instrumented) -> None:
    instrumented()
    app = ApertureMiddleware(build_app())

    upstream = SpanContext(
        trace_id=0xABCDEF00112233445566778899AABBCC,
        span_id=0x1234567890ABCDEF,
        parent_span_id=0,
        sampled=True,
    )
    await call(app, "/ping", headers={"traceparent": format_traceparent(upstream)})

    span = server_spans(aperture.drain_buffered_spans())[0]
    assert span.trace_id == upstream.trace_id
    assert span.parent_span_id == upstream.span_id
    assert span.span_id != upstream.span_id


async def test_a_malformed_traceparent_starts_a_fresh_trace(instrumented) -> None:
    instrumented()
    app = ApertureMiddleware(build_app())

    response = await call(app, "/ping", headers={"traceparent": "not-a-header"})
    assert response.status_code == 200

    span = server_spans(aperture.drain_buffered_spans())[0]
    assert span.trace_id > 0
    assert span.parent_span_id == 0


async def test_an_unsampled_upstream_decision_is_honoured(instrumented) -> None:
    """If the caller decided not to sample, we do not overrule it."""
    instrumented()
    app = ApertureMiddleware(build_app())

    upstream = SpanContext(
        trace_id=0xAA, span_id=0xBB, parent_span_id=0, sampled=False
    )
    response = await call(
        app, "/ping", headers={"traceparent": format_traceparent(upstream)}
    )
    assert response.status_code == 200
    assert server_spans(aperture.drain_buffered_spans()) == []


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


async def test_query_strings_are_measured_not_captured(instrumented) -> None:
    """Query strings carry tokens and email addresses. Size only."""
    instrumented()
    app = ApertureMiddleware(build_app())

    await call(app, "/ping?api_key=super-secret-value&email=a@b.com")

    span = server_spans(aperture.drain_buffered_spans())[0]
    serialised = repr(span)
    assert "super-secret-value" not in serialised
    assert "a@b.com" not in serialised
    assert int(span.attributes["aperture.http.query_bytes"]) > 0


# ---------------------------------------------------------------------------
# Non-HTTP scopes and the disabled path
# ---------------------------------------------------------------------------


async def test_non_http_scopes_pass_straight_through(instrumented) -> None:
    instrumented()
    seen = []

    async def bare_app(scope, receive, send):
        seen.append(scope["type"])

    app = ApertureMiddleware(bare_app)
    await app({"type": "lifespan"}, None, None)
    await app({"type": "websocket", "path": "/ws"}, None, None)

    assert seen == ["lifespan", "websocket"]
    assert aperture.drain_buffered_spans() == []


async def test_the_middleware_is_inert_when_the_sdk_is_disabled() -> None:
    """No tracer means the wrapper is a single extra await and nothing else."""
    assert not aperture.is_enabled()
    app = ApertureMiddleware(build_app())

    response = await call(app, "/ping")
    assert response.status_code == 200
    assert aperture.drain_buffered_spans() == []


async def test_instrument_app_adds_no_middleware_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("APERTURE_SDK_ENABLED", raising=False)
    app = build_app()
    before = len(app.user_middleware)

    returned = aperture.instrument_app(app)

    assert returned is app
    assert len(app.user_middleware) == before


async def test_instrument_app_wraps_a_bare_asgi_callable(instrumented) -> None:
    instrumented()

    async def bare_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = aperture.instrument_app(bare_app)
    assert isinstance(wrapped, ApertureMiddleware)

    response = await call(wrapped, "/anything")
    assert response.status_code == 204
    assert len(server_spans(aperture.drain_buffered_spans())) == 1


# ---------------------------------------------------------------------------
# Diagnostics endpoint
# ---------------------------------------------------------------------------


async def test_the_stats_path_is_off_by_default(instrumented) -> None:
    instrumented()
    app = ApertureMiddleware(build_app())
    response = await call(app, "/aperture/stats")
    assert response.status_code == 404


async def test_the_stats_path_reports_drop_counters_when_enabled(instrumented) -> None:
    """Dropped spans have to be observable from outside the process."""
    config = instrumented(stats_path="/aperture/stats", buffer_capacity=2)
    app = ApertureMiddleware(build_app(), config=config)

    for _ in range(6):
        await call(app, "/ping")

    response = await call(app, "/aperture/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["buffer_capacity"] == 2
    assert body["spans_dropped_buffer_full"] > 0

    aperture.drain_buffered_spans()


async def test_the_stats_path_is_not_itself_traced(instrumented) -> None:
    config = instrumented(stats_path="/aperture/stats")
    app = ApertureMiddleware(build_app(), config=config)

    await call(app, "/aperture/stats")
    assert aperture.drain_buffered_spans() == []


# ---------------------------------------------------------------------------
# Overflow under load
# ---------------------------------------------------------------------------


async def test_requests_still_succeed_when_the_buffer_is_full(instrumented) -> None:
    """The whole point of the bound: telemetry degrades, the app does not."""
    instrumented(buffer_capacity=3)
    app = ApertureMiddleware(build_app())

    for _ in range(25):
        response = await call(app, "/ping")
        assert response.status_code == 200

    stats = aperture.get_stats()
    assert stats["spans_dropped_buffer_full"] > 0
    assert stats["buffer_size"] <= 3
    aperture.drain_buffered_spans()
