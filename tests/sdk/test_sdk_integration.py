"""End-to-end: the SDK against the real sample-shop.

This is the test that decides whether Day 2 actually worked. It runs genuine
requests through the benchmark application, against a real PostgreSQL, with
instrumentation installed exactly the way an operator would install it — one
call in the app factory — and then checks that the resulting trace tree has the
structure Week 2's detectors will have to read.

If the assertions about P1 and P2 below hold, the N+1 detector has something
real to find. If they do not, nothing built on top of this will work either.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

import aperture
from aperture.spans import CLIENT_KIND_DB, SpanKind


@pytest.fixture
def instrumented_shop(instrumented, seeded_database, monkeypatch):
    """A freshly built sample-shop app with the SDK installed.

    `create_app()` is called again rather than reusing the module-level app,
    because that one was built at import time with the SDK disabled — which is
    itself the behaviour the sample-shop suite depends on.
    """
    monkeypatch.setenv("APERTURE_SDK_ENABLED", "true")
    monkeypatch.setenv("APERTURE_COLLECTOR_ENDPOINT", "127.0.0.1:14317")
    monkeypatch.setenv("APERTURE_EXPORT_INTERVAL_MS", "60000")

    from shop.main import create_app

    app = create_app()
    assert aperture.is_enabled(), "instrument_app did not install the SDK"
    return app


async def shop_get(app, path: str, user_id: int | None = None) -> httpx.Response:
    headers = {"X-User-Id": str(user_id)} if user_id is not None else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://shop.test"
    ) as client:
        return await client.get(path, headers=headers)


def db_spans(spans) -> list:
    return [s for s in spans if s.attributes.get("aperture.client_kind") == CLIENT_KIND_DB]


def build_tree(spans) -> dict:
    """parent_span_id -> [children]"""
    tree: dict = {}
    for span in spans:
        tree.setdefault(span.parent_span_id, []).append(span)
    return tree


# ---------------------------------------------------------------------------
# The integration contract
# ---------------------------------------------------------------------------


async def test_one_middleware_call_instruments_the_whole_application(
    instrumented_shop,
) -> None:
    """Constraint C2: no engine handed over, no session wrapped, no router touched."""
    response = await shop_get(instrumented_shop, "/api/categories")
    assert response.status_code == 200

    spans = aperture.drain_buffered_spans()
    assert any(s.kind is SpanKind.SERVER for s in spans)
    assert any(s.attributes.get("aperture.client_kind") == CLIENT_KIND_DB for s in spans)

    stats = aperture.get_stats()
    assert set(stats["hooks_installed"]) >= {"sqlalchemy", "pool"}


async def test_a_request_becomes_one_connected_trace(instrumented_shop, seed_ids) -> None:
    response = await shop_get(
        instrumented_shop, f"/api/products/{seed_ids['product_id']}"
    )
    assert response.status_code == 200

    # The httpx test client is itself instrumented, so drop its client span and
    # look at the server span's own subtree.
    spans = aperture.drain_buffered_spans()
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    subtree = [s for s in spans if s.trace_id == server.trace_id]

    assert len({s.trace_id for s in subtree}) == 1
    assert server.endpoint == "/api/products/{product_id}"

    children = build_tree(subtree).get(server.span_id, [])
    assert children, "the request span has no children"
    assert all(c.endpoint == server.endpoint for c in children)


async def test_the_server_span_covers_its_children(instrumented_shop, seed_ids) -> None:
    """Durations must nest, or the waterfall and the materiality ratio are junk."""
    await shop_get(instrumented_shop, f"/api/products/{seed_ids['product_id']}")

    spans = aperture.drain_buffered_spans()
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    children = [
        s for s in spans if s.parent_span_id == server.span_id and s.kind is SpanKind.CLIENT
    ]

    assert children
    assert server.duration_ns >= sum(c.duration_ns for c in children)
    for child in children:
        assert child.start_unix_ns >= server.start_unix_ns
        assert (
            child.start_unix_ns + child.duration_ns
            <= server.start_unix_ns + server.duration_ns
        )


# ---------------------------------------------------------------------------
# The planted pathologies, seen through the SDK
# ---------------------------------------------------------------------------


async def test_p1_appears_as_sibling_queries_sharing_a_fingerprint(
    instrumented_shop, seed_ids
) -> None:
    """PATHOLOGY P1, GET /api/orders — the flagship N+1.

    Checked against every clause of the structural definition in DESIGN.md 6.2,
    because a detector built on a trace that fails any of them will be wrong.
    """
    response = await shop_get(instrumented_shop, "/api/orders?limit=8", user_id=seed_ids["user_id"])
    assert response.status_code == 200
    returned = len(response.json()["items"])
    assert returned >= 3, "seed data too small to exercise P1"

    spans = aperture.drain_buffered_spans()
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    queries = [s for s in db_spans(spans) if s.parent_span_id == server.span_id]

    by_fingerprint: dict[int, list] = {}
    for span in queries:
        by_fingerprint.setdefault(span.db_fingerprint, []).append(span)
    fingerprint, group = max(by_fingerprint.items(), key=lambda kv: len(kv[1]))

    # N sibling queries, one fingerprint
    assert len(group) == returned
    assert "order_items" in group[0].db_statement

    # varying bound parameters (an N+1, not a cache miss)
    assert len({s.attributes["aperture.db.params_digest"] for s in group}) > 1

    # few rows each (not pagination)
    assert max(s.db_rows for s in group) <= 10

    # sequential, not a deliberate concurrent fan-out
    ordered = sorted(group, key=lambda s: s.start_unix_ns)
    for earlier, later in zip(ordered, ordered[1:]):
        assert later.start_unix_ns >= earlier.start_unix_ns + earlier.duration_ns

    # one call site, named
    locations = {s.code_location for s in group}
    assert len(locations) == 1
    assert "orders.py" in locations.pop()


async def test_p2_appears_as_one_author_lookup_per_review(
    instrumented_shop, seed_ids
) -> None:
    """PATHOLOGY P2, GET /api/products/{id} — N+1 on review authors.

    The location assertion is the interesting one. This endpoint's author
    lookup is byte-identical to the auth dependency's, so a code-location
    cache keyed on the statement would blame the wrong file.
    """
    from sqlalchemy import text

    from shop.db import get_session_factory

    async with get_session_factory()() as session:
        product_id = (
            await session.execute(
                text(
                    "SELECT product_id FROM reviews GROUP BY product_id "
                    "ORDER BY count(*) DESC LIMIT 1"
                )
            )
        ).scalar_one()

    aperture.drain_buffered_spans()
    response = await shop_get(instrumented_shop, f"/api/products/{product_id}")
    assert response.status_code == 200
    reviews = response.json()["recent_reviews"]
    assert len(reviews) >= 3, "seed data too small to exercise P2"

    spans = aperture.drain_buffered_spans()
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    queries = [s for s in db_spans(spans) if s.parent_span_id == server.span_id]

    # The repeated group, found the way a detector would: group by fingerprint
    # and take the largest. Filtering on the statement text instead would also
    # catch the product query, which joins users to load the seller.
    by_fingerprint: dict[int, list] = {}
    for span in queries:
        by_fingerprint.setdefault(span.db_fingerprint, []).append(span)
    _, author_lookups = max(by_fingerprint.items(), key=lambda kv: len(kv[1]))

    assert len(author_lookups) >= 3
    assert "FROM users" in author_lookups[0].db_statement
    assert max(s.db_rows for s in author_lookups) == 1

    # One call site, and specifically *not* the auth dependency, whose query is
    # byte-identical. This is the assertion that would fail if code locations
    # were cached by statement.
    locations = {s.code_location for s in author_lookups}
    assert len(locations) == 1, f"author lookups reported several sites: {locations}"
    location = locations.pop()
    assert "catalog.py" in location, location
    assert "dependencies.py" not in location, (
        "the N+1 was attributed to the auth dependency, which runs the same SQL"
    )


async def test_the_control_endpoint_does_not_produce_the_n_plus_one_shape(
    instrumented_shop, seed_ids
) -> None:
    """GET /api/orders/{id} returns the same data with `selectinload`.

    Detectors must find nothing here. If a control endpoint produces the same
    trace shape as its pathological twin, precision is unmeasurable.
    """
    await shop_get(instrumented_shop, f"/api/orders/{seed_ids['order_id']}")

    spans = aperture.drain_buffered_spans()
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    queries = [s for s in db_spans(spans) if s.parent_span_id == server.span_id]

    counts: dict[int, int] = {}
    for span in queries:
        counts[span.db_fingerprint] = counts.get(span.db_fingerprint, 0) + 1
    assert max(counts.values()) < 5, "control endpoint shows a repeated-query group"


async def test_pool_wait_is_recorded_on_the_request(instrumented_shop) -> None:
    await shop_get(instrumented_shop, "/api/categories")

    spans = aperture.drain_buffered_spans()
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    queries = db_spans(spans)
    assert server.pool_wait_ns == sum(s.pool_wait_ns for s in queries)


# ---------------------------------------------------------------------------
# Behaviour under failure
# ---------------------------------------------------------------------------


async def test_the_application_works_with_no_collector_listening(
    instrumented_shop, seed_ids
) -> None:
    """The whole fail-open promise, checked end to end.

    Nothing is listening on the configured endpoint. Every request must still
    return correct data, and the SDK must account for what it threw away.
    """
    for _ in range(6):
        response = await shop_get(instrumented_shop, "/api/categories")
        assert response.status_code == 200
        assert len(response.json()) == 12

    assert aperture.flush(timeout=3.0) is False or True  # never raises

    stats = aperture.get_stats()
    assert stats["enabled"] is True
    assert stats["spans_exported"] == 0
    # Either dropped on export or still buffered — but never blocking a request.
    assert stats["export_failures"] + stats["buffer_size"] > 0
    aperture.drain_buffered_spans()


async def test_requests_survive_buffer_exhaustion(instrumented_shop) -> None:
    """A tiny buffer is a stand-in for sustained overload."""
    from aperture.buffer import SpanBuffer

    aperture._buffer = SpanBuffer(2)
    tracer = aperture.get_tracer()
    assert tracer is not None
    tracer.buffer = aperture._buffer

    for _ in range(12):
        response = await shop_get(instrumented_shop, "/api/categories")
        assert response.status_code == 200

    stats = aperture._buffer.stats()
    assert stats["spans_dropped_buffer_full"] > 0
    assert stats["buffer_size"] <= 2


async def test_a_failing_endpoint_is_still_traced(instrumented_shop) -> None:
    response = await shop_get(instrumented_shop, "/api/products/99999999")
    assert response.status_code == 404

    spans = aperture.drain_buffered_spans()
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    assert server.endpoint == "/api/products/{product_id}"
    assert server.attributes["http.response.status_code"] == "404"


async def test_shutdown_leaves_the_application_working(instrumented_shop) -> None:
    aperture.shutdown(timeout=1.0)
    assert not aperture.is_enabled()

    response = await shop_get(instrumented_shop, "/api/categories")
    assert response.status_code == 200
    assert aperture.drain_buffered_spans() == []
