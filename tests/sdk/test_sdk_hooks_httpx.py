"""Outbound HTTP instrumentation, against a real local HTTP server.

Not an ASGI transport: a genuine socket, so the spans have real network time in
them and the `traceparent` header is verified as it actually arrives on the
wire — which is the thing that has to work for a trace to cross a service
boundary.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import aperture
from aperture.context import parse_traceparent
from aperture.hooks.httpx import normalize_url_path
from aperture.spans import CLIENT_KIND_HTTP, SpanKind, SpanStatus, get_tracer

RECEIVED_HEADERS: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        RECEIVED_HEADERS.append({k.lower(): v for k, v in self.headers.items()})
        status = 500 if self.path.startswith("/fail") else 200
        body = b'{"ok": true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the test output readable
        pass


@pytest.fixture
def http_server():
    RECEIVED_HEADERS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def http_spans(spans) -> list:
    return [
        s for s in spans if s.attributes.get("aperture.client_kind") == CLIENT_KIND_HTTP
    ]


# ---------------------------------------------------------------------------
# URL templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/products/42/reviews", "/api/products/{id}/reviews"),
        ("/api/products/42", "/api/products/{id}"),
        ("/api/orders", "/api/orders"),
        ("/api/users/550e8400e29b41d4a716446655440000", "/api/users/{id}"),
        ("/api/users/550e8400-e29b-41d4-a716-446655440000", "/api/users/{id}"),
        ("/", "/"),
        ("", "/"),
    ],
)
def test_url_paths_collapse_to_templates(path: str, expected: str) -> None:
    assert normalize_url_path(path) == expected


def test_slug_segments_are_not_collapsed() -> None:
    """A documented weakness of the heuristic, pinned so it stays known.

    The real answer is the callee's own route table, which is multi-service
    work and belongs with pathology P8.
    """
    assert normalize_url_path("/api/products/blue-widget") == "/api/products/blue-widget"


# ---------------------------------------------------------------------------
# Span capture
# ---------------------------------------------------------------------------


async def test_an_outbound_request_produces_a_client_span(
    instrumented, http_server
) -> None:
    instrumented()
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_server}/api/products/42")
    assert response.status_code == 200

    spans = http_spans(aperture.drain_buffered_spans())
    assert len(spans) == 1
    span = spans[0]
    assert span.kind is SpanKind.CLIENT
    assert span.status is SpanStatus.OK
    assert span.operation == "http.get"
    assert span.duration_ns > 0
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.response.status_code"] == "200"
    assert span.attributes["aperture.http.url_template"] == "/api/products/{id}"
    assert span.attributes["server.address"] == "127.0.0.1"


async def test_the_url_template_method_is_labelled_as_a_placeholder(
    instrumented, http_server
) -> None:
    instrumented()
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_server}/api/products/42")

    (span,) = http_spans(aperture.drain_buffered_spans())
    assert "placeholder" in span.attributes["aperture.http.url_template_method"]


async def test_query_strings_are_stripped_from_the_recorded_url(
    instrumented, http_server
) -> None:
    """Query strings carry API keys often enough to be a liability."""
    instrumented()
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_server}/api/x?token=super-secret&email=a@b.com")

    (span,) = http_spans(aperture.drain_buffered_spans())
    assert "super-secret" not in repr(span)
    assert "a@b.com" not in repr(span)
    assert span.attributes["url.full"].endswith("/api/x")


async def test_the_code_location_of_the_call_is_captured(
    instrumented, http_server
) -> None:
    instrumented()
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_server}/api/x")

    (span,) = http_spans(aperture.drain_buffered_spans())
    assert "test_sdk_hooks_httpx.py" in span.code_location


def test_the_synchronous_client_is_instrumented_too(instrumented, http_server) -> None:
    instrumented()
    with httpx.Client() as client:
        response = client.get(f"{http_server}/api/products/7")
    assert response.status_code == 200

    (span,) = http_spans(aperture.drain_buffered_spans())
    assert span.attributes["aperture.http.url_template"] == "/api/products/{id}"


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------


async def test_traceparent_reaches_the_callee(instrumented, http_server) -> None:
    """The header is read off the wire by the receiving server, not from the
    request object we mutated."""
    instrumented()
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_server}/api/x")

    assert RECEIVED_HEADERS, "the server received no request"
    header = RECEIVED_HEADERS[-1].get("traceparent")
    assert header, "no traceparent was sent"

    parsed = parse_traceparent(header)
    assert parsed is not None

    (span,) = http_spans(aperture.drain_buffered_spans())
    assert parsed.trace_id == span.trace_id
    assert parsed.parent_span_id == span.span_id


async def test_the_client_span_joins_the_surrounding_trace(
    instrumented, http_server
) -> None:
    instrumented()
    tracer = get_tracer()
    assert tracer is not None

    with tracer.span("handler") as parent:
        async with httpx.AsyncClient() as client:
            await client.get(f"{http_server}/api/x")

    spans = aperture.drain_buffered_spans()
    handler = next(s for s in spans if s.operation == "handler")
    (client_span,) = http_spans(spans)
    assert client_span.parent_span_id == handler.span_id
    assert client_span.trace_id == handler.trace_id


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_a_5xx_response_marks_the_span_as_an_error(
    instrumented, http_server
) -> None:
    instrumented()
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_server}/fail")
    assert response.status_code == 500

    (span,) = http_spans(aperture.drain_buffered_spans())
    assert span.status is SpanStatus.ERROR
    assert span.attributes["http.response.status_code"] == "500"


async def test_a_connection_failure_records_and_re_raises(instrumented) -> None:
    instrumented()
    async with httpx.AsyncClient() as client:
        # TransportError, not ConnectError: an unreachable port surfaces as a
        # refusal on some platforms and a timeout on others (Windows gives
        # ConnectTimeout here). Either way the span must be closed as an error
        # and the exception must reach the caller unchanged.
        with pytest.raises(httpx.TransportError):
            await client.get("http://127.0.0.1:1/nothing-here", timeout=2.0)

    spans = http_spans(aperture.drain_buffered_spans())
    assert len(spans) == 1
    assert spans[0].status is SpanStatus.ERROR
    assert spans[0].status_message


# ---------------------------------------------------------------------------
# The chatty fan-out shape (pathology P8)
# ---------------------------------------------------------------------------


async def test_repeated_calls_share_a_url_template(instrumented, http_server) -> None:
    """The HTTP analogue of an N+1: one template, many sequential calls.

    D1 generalises to this by grouping on the template instead of a SQL
    fingerprint, so the grouping key has to collapse the ids.
    """
    instrumented()
    tracer = get_tracer()
    assert tracer is not None

    with tracer.span("dashboard") as parent:
        async with httpx.AsyncClient() as client:
            for product_id in range(1, 7):
                await client.get(f"{http_server}/api/products/{product_id}/stats")

    spans = aperture.drain_buffered_spans()
    dashboard = next(s for s in spans if s.operation == "dashboard")
    calls = http_spans(spans)

    assert len(calls) == 6
    assert {s.attributes["aperture.http.url_template"] for s in calls} == {
        "/api/products/{id}/stats"
    }
    assert all(s.parent_span_id == dashboard.span_id for s in calls)

    ordered = sorted(calls, key=lambda s: s.start_unix_ns)
    for earlier, later in zip(ordered, ordered[1:]):
        assert later.start_unix_ns >= earlier.start_unix_ns + earlier.duration_ns


async def test_concurrent_calls_overlap_in_time(instrumented, http_server) -> None:
    """The counter-case D1's non-overlap guard exists to exclude.

    A deliberate `asyncio.gather` fan-out must come out overlapping, so the
    detector can tell it apart from the sequential loop above and not cry wolf.
    """
    import asyncio

    instrumented()
    tracer = get_tracer()
    assert tracer is not None

    with tracer.span("gathered"):
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                *(
                    client.get(f"{http_server}/api/products/{i}/stats")
                    for i in range(1, 7)
                )
            )

    calls = http_spans(aperture.drain_buffered_spans())
    assert len(calls) == 6

    latest_start = max(s.start_unix_ns for s in calls)
    earliest_end = min(s.start_unix_ns + s.duration_ns for s in calls)
    assert latest_start < earliest_end, "concurrent calls did not overlap"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_send_is_restored_on_uninstall(http_server) -> None:
    original_async = httpx.AsyncClient.send
    original_sync = httpx.Client.send

    aperture.instrument(
        aperture.ApertureConfig(enabled=True, collector_endpoint="127.0.0.1:14317")
    )
    assert httpx.AsyncClient.send is not original_async

    aperture.shutdown(timeout=0.5)
    assert httpx.AsyncClient.send is original_async
    assert httpx.Client.send is original_sync

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_server}/api/x")
    assert response.status_code == 200
    assert aperture.drain_buffered_spans() == []
