"""OTLP encoding and export, including what happens when nobody is listening."""

from __future__ import annotations

import time

import grpc
import pytest

from aperture.buffer import SpanBuffer
from aperture.exporter import (
    ATTR_CODE_LOCATION,
    ATTR_DB_FINGERPRINT,
    ATTR_DB_ROWS,
    ATTR_DB_STATEMENT,
    ATTR_HTTP_ROUTE,
    ATTR_POOL_WAIT_NS,
    OtlpSpanExporter,
    encode_batch,
    encode_span,
)
from aperture.spans import Span, SpanKind, SpanStatus


def make_span(**overrides) -> Span:
    defaults = dict(
        trace_id=0x0123456789ABCDEF0123456789ABCDEF,
        span_id=0x1122334455667788,
        parent_span_id=0x8877665544332211,
        service="test-service",
        operation="db.select",
        kind=SpanKind.CLIENT,
        start_unix_ns=1_700_000_000_000_000_000,
        duration_ns=5_000_000,
        status=SpanStatus.OK,
    )
    defaults.update(overrides)
    return Span(**defaults)  # type: ignore[arg-type]


def attrs_of(pb_span) -> dict:
    out = {}
    for kv in pb_span.attributes:
        value = kv.value
        out[kv.key] = (
            value.string_value if value.HasField("string_value") else value.int_value
        )
    return out


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def test_identifiers_encode_to_the_widths_otlp_requires() -> None:
    encoded = encode_span(make_span())
    assert len(encoded.trace_id) == 16
    assert len(encoded.span_id) == 8
    assert len(encoded.parent_span_id) == 8
    assert encoded.trace_id.hex() == "0123456789abcdef0123456789abcdef"


def test_a_root_span_sends_an_empty_parent_not_eight_zero_bytes() -> None:
    """OTLP distinguishes 'no parent' from 'a parent whose id is zero'."""
    encoded = encode_span(make_span(parent_span_id=0))
    assert encoded.parent_span_id == b""


def test_timestamps_and_kind_survive_encoding() -> None:
    span = make_span(start_unix_ns=1_000, duration_ns=250)
    encoded = encode_span(span)
    assert encoded.start_time_unix_nano == 1_000
    assert encoded.end_time_unix_nano == 1_250
    assert encoded.kind == int(SpanKind.CLIENT)


def test_status_encodes_with_its_message() -> None:
    encoded = encode_span(
        make_span(status=SpanStatus.ERROR, status_message="deadlock detected")
    )
    assert encoded.status.code == int(SpanStatus.ERROR)
    assert encoded.status.message == "deadlock detected"


def test_every_documented_column_reaches_the_wire() -> None:
    """The attribute contract the Go collector will decode."""
    span = make_span(
        endpoint="/api/orders",
        db_statement="SELECT * FROM order_items WHERE order_id = $1",
        db_fingerprint=42,
        db_fingerprint_method="placeholder/x",
        db_rows=3,
        pool_wait_ns=1_500_000,
        code_location="shop/routers/orders.py:95",
    )
    span.attributes["aperture.client_kind"] = "db"

    attributes = attrs_of(encode_span(span))
    assert attributes[ATTR_HTTP_ROUTE] == "/api/orders"
    assert attributes[ATTR_DB_STATEMENT].startswith("SELECT * FROM order_items")
    assert attributes[ATTR_DB_FINGERPRINT] == 42
    assert attributes[ATTR_DB_ROWS] == 3
    assert attributes[ATTR_POOL_WAIT_NS] == 1_500_000
    assert attributes[ATTR_CODE_LOCATION] == "shop/routers/orders.py:95"
    assert attributes["aperture.client_kind"] == "db"


def test_an_unknown_row_count_is_omitted_rather_than_sent_as_zero() -> None:
    attributes = attrs_of(encode_span(make_span()))
    assert ATTR_DB_ROWS not in attributes


def test_numeric_fields_are_encoded_as_integers_not_strings() -> None:
    """The collector maps these to UInt columns; strings would need parsing."""
    encoded = encode_span(make_span(db_fingerprint=7, db_rows=9, pool_wait_ns=11))
    for kv in encoded.attributes:
        if kv.key in (ATTR_DB_FINGERPRINT, ATTR_DB_ROWS, ATTR_POOL_WAIT_NS):
            assert kv.value.HasField("int_value")


# ---------------------------------------------------------------------------
# int64 range — a bug that only a real wire encoder could find
# ---------------------------------------------------------------------------


def test_real_fingerprints_fit_in_a_signed_int64() -> None:
    """OTLP integer attributes are `int64`, which is SIGNED.

    A full 64-bit hash overflows it roughly half the time, and protobuf does
    not fail politely: it raises while serialising the whole message. Against
    the live sink that turned one oversized fingerprint into several thousand
    dropped spans. Fingerprints are masked to 63 bits for this reason.
    """
    from aperture.hooks import placeholder_fingerprint
    from aperture.hooks.httpx import _template_fingerprint
    from aperture.hooks.sqlalchemy import _params_digest

    for i in range(500):
        assert 0 <= placeholder_fingerprint(f"SELECT {i} FROM t WHERE x = {i}") <= 2**63 - 1
        assert 0 <= _params_digest((i, f"value-{i}")) <= 2**63 - 1
        assert 0 <= _template_fingerprint("GET", f"/api/x/{i}/y") <= 2**63 - 1


def test_a_batch_of_realistic_fingerprints_serialises(sdk_config) -> None:
    """The end-to-end version: build a batch the way the hooks would, and
    actually serialise it. This is the assertion that would have caught it."""
    from aperture.hooks import placeholder_fingerprint

    spans = [
        make_span(
            operation="db.select",
            db_fingerprint=placeholder_fingerprint(
                f"SELECT * FROM order_items WHERE order_id = ${i}"
            ),
            db_fingerprint_method="placeholder/x",
        )
        for i in range(200)
    ]
    request = encode_batch(sdk_config(), spans)
    assert len(request.SerializeToString()) > 0


def test_an_oversized_integer_falls_back_to_a_string(sdk_config) -> None:
    """Backstop for any future producer that forgets to mask."""
    span = make_span(db_fingerprint=2**64 - 1, db_fingerprint_method="broken")
    encoded = encode_span(span)
    assert encoded.SerializeToString()
    values = {kv.key: kv.value for kv in encoded.attributes}
    assert values[ATTR_DB_FINGERPRINT].HasField("string_value")


def test_one_unencodable_span_does_not_take_the_batch_with_it(sdk_config) -> None:
    """Losing one span is a bad day. Losing the batch is an outage in the data."""
    good = [make_span(operation=f"ok-{i}") for i in range(5)]
    broken = make_span(operation="bad")
    broken.start_unix_ns = -1  # negative uint64 field: protobuf refuses it

    request = encode_batch(sdk_config(), good[:2] + [broken] + good[2:])
    names = {s.name for s in request.resource_spans[0].scope_spans[0].spans}
    assert names == {f"ok-{i}" for i in range(5)}


def test_batch_carries_resource_identity(sdk_config) -> None:
    config = sdk_config(service_name="sample-shop", service_version="1.2.3")
    request = encode_batch(config, [make_span(), make_span()])

    (resource_spans,) = request.resource_spans
    resource = {
        kv.key: kv.value.string_value for kv in resource_spans.resource.attributes
    }
    assert resource["service.name"] == "sample-shop"
    assert resource["service.version"] == "1.2.3"
    assert resource["telemetry.sdk.language"] == "python"

    (scope_spans,) = resource_spans.scope_spans
    assert scope_spans.scope.name == "aperture-sdk"
    assert len(scope_spans.spans) == 2


def test_an_empty_batch_encodes_without_error(sdk_config) -> None:
    request = encode_batch(sdk_config(), [])
    assert len(request.resource_spans[0].scope_spans[0].spans) == 0


# ---------------------------------------------------------------------------
# Export against a real collector
# ---------------------------------------------------------------------------


def test_spans_reach_a_real_otlp_collector(sdk_config, collector) -> None:
    servicer, endpoint = collector
    config = sdk_config(collector_endpoint=endpoint, export_interval_ms=20)
    buffer = SpanBuffer(64)
    exporter = OtlpSpanExporter(config, buffer)
    exporter.start()
    try:
        for i in range(5):
            buffer.put(make_span(operation=f"op-{i}"))
        assert exporter.flush(timeout=5.0), "exporter did not drain the buffer"
    finally:
        exporter.shutdown(timeout=2.0)

    assert exporter.spans_exported == 5
    assert exporter.export_failures == 0
    assert {s.name for s in servicer.spans} == {f"op-{i}" for i in range(5)}


def test_large_backlogs_are_sent_in_batches(sdk_config, collector) -> None:
    servicer, endpoint = collector
    config = sdk_config(
        collector_endpoint=endpoint, export_interval_ms=20, export_batch_size=10
    )
    buffer = SpanBuffer(256)
    exporter = OtlpSpanExporter(config, buffer)
    exporter.start()
    try:
        for i in range(95):
            buffer.put(make_span(operation=f"op-{i}"))
        assert exporter.flush(timeout=5.0)
    finally:
        exporter.shutdown(timeout=2.0)

    assert exporter.spans_exported == 95
    assert len(servicer.requests) == 10  # 9 full batches plus a partial one
    assert len(servicer.spans) == 95


# ---------------------------------------------------------------------------
# Collector unavailable — the fail-open requirement
# ---------------------------------------------------------------------------


def test_collector_unavailable_drops_spans_and_counts_them(
    sdk_config, dead_collector
) -> None:
    """Nothing is listening. The SDK must shrug and keep the counters honest."""
    config = sdk_config(
        collector_endpoint=dead_collector,
        export_interval_ms=20,
        export_timeout_s=0.5,
    )
    buffer = SpanBuffer(64)
    exporter = OtlpSpanExporter(config, buffer)
    exporter.start()
    try:
        for i in range(8):
            buffer.put(make_span(operation=f"op-{i}"))
        exporter.flush(timeout=4.0)
    finally:
        exporter.shutdown(timeout=1.0)

    assert exporter.export_failures >= 1
    assert exporter.spans_exported == 0
    assert exporter.spans_dropped_export_failure >= 8
    assert "" != exporter.last_error


def test_a_failing_collector_does_not_grow_memory(sdk_config, dead_collector) -> None:
    """There is no retry queue, on purpose.

    A queue of un-exported batches that grows while the collector is down is
    precisely the unbounded-memory failure constraint C3 forbids. Failed
    batches are dropped, and the ring stays at its fixed capacity.
    """
    config = sdk_config(
        collector_endpoint=dead_collector,
        export_interval_ms=20,
        export_timeout_s=0.3,
        export_batch_size=4,
    )
    buffer = SpanBuffer(16)
    exporter = OtlpSpanExporter(config, buffer)
    exporter.start()
    try:
        for _ in range(6):
            for i in range(16):
                buffer.put(make_span(operation=f"op-{i}"))
            time.sleep(0.05)
        assert len(buffer) <= 16
    finally:
        exporter.shutdown(timeout=1.0)

    assert buffer.capacity == 16
    assert exporter.spans_dropped_export_failure > 0


def test_a_rejecting_collector_is_treated_as_a_failure(sdk_config, collector) -> None:
    """A collector that answers with an error is not a collector that worked."""
    servicer, endpoint = collector
    servicer.fail_with = grpc.StatusCode.RESOURCE_EXHAUSTED

    config = sdk_config(collector_endpoint=endpoint, export_interval_ms=20)
    buffer = SpanBuffer(32)
    exporter = OtlpSpanExporter(config, buffer)
    exporter.start()
    try:
        for i in range(4):
            buffer.put(make_span(operation=f"op-{i}"))
        exporter.flush(timeout=4.0)
    finally:
        exporter.shutdown(timeout=1.0)

    assert exporter.spans_exported == 0
    assert exporter.export_failures >= 1
    assert exporter.spans_dropped_export_failure >= 4


def test_export_recovers_when_the_collector_comes_back(sdk_config, collector) -> None:
    servicer, endpoint = collector
    servicer.fail_with = grpc.StatusCode.UNAVAILABLE

    config = sdk_config(
        collector_endpoint=endpoint,
        export_interval_ms=20,
        export_failure_backoff_after=1,
        export_backoff_max_s=0.2,
    )
    buffer = SpanBuffer(32)
    exporter = OtlpSpanExporter(config, buffer)
    exporter.start()
    try:
        buffer.put(make_span(operation="before"))
        exporter.flush(timeout=3.0)
        assert exporter.consecutive_failures >= 1

        servicer.fail_with = None
        buffer.put(make_span(operation="after"))
        assert exporter.flush(timeout=5.0)
    finally:
        exporter.shutdown(timeout=1.0)

    assert exporter.consecutive_failures == 0
    assert "after" in {s.name for s in servicer.spans}


def test_backoff_grows_with_consecutive_failures(sdk_config, dead_collector) -> None:
    config = sdk_config(
        collector_endpoint=dead_collector,
        export_interval_ms=100,
        export_failure_backoff_after=2,
        export_backoff_max_s=1.0,
    )
    exporter = OtlpSpanExporter(config, SpanBuffer(8))

    exporter.consecutive_failures = 2
    first = exporter._backoff_seconds()
    exporter.consecutive_failures = 4
    later = exporter._backoff_seconds()

    assert later > first
    assert later <= config.export_backoff_max_s


def test_backoff_is_capped(sdk_config, dead_collector) -> None:
    config = sdk_config(
        collector_endpoint=dead_collector,
        export_interval_ms=1000,
        export_failure_backoff_after=1,
        export_backoff_max_s=5.0,
    )
    exporter = OtlpSpanExporter(config, SpanBuffer(8))
    exporter.consecutive_failures = 500
    assert exporter._backoff_seconds() == 5.0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_the_exporter_thread_is_a_daemon(sdk_config, dead_collector) -> None:
    """A wedged exporter must never stop the process from exiting."""
    exporter = OtlpSpanExporter(sdk_config(collector_endpoint=dead_collector), SpanBuffer(8))
    exporter.start()
    try:
        assert exporter._thread is not None
        assert exporter._thread.daemon is True
    finally:
        exporter.shutdown(timeout=1.0)


def test_start_is_idempotent(sdk_config, dead_collector) -> None:
    exporter = OtlpSpanExporter(sdk_config(collector_endpoint=dead_collector), SpanBuffer(8))
    exporter.start()
    thread = exporter._thread
    exporter.start()
    try:
        assert exporter._thread is thread
    finally:
        exporter.shutdown(timeout=1.0)


def test_shutdown_flushes_what_is_left(sdk_config, collector) -> None:
    servicer, endpoint = collector
    config = sdk_config(collector_endpoint=endpoint, export_interval_ms=60_000)
    buffer = SpanBuffer(32)
    exporter = OtlpSpanExporter(config, buffer)
    exporter.start()
    for i in range(3):
        buffer.put(make_span(operation=f"op-{i}"))

    assert exporter.shutdown(timeout=5.0) is True
    assert len(servicer.spans) == 3


def test_shutdown_without_start_is_safe(sdk_config) -> None:
    assert OtlpSpanExporter(sdk_config(), SpanBuffer(4)).shutdown(timeout=0.1) is True


def test_creating_the_exporter_does_no_network_io(sdk_config) -> None:
    """Construction must not connect: gRPC channels are lazy, and startup
    must not block on a collector that may not exist yet."""
    exporter = OtlpSpanExporter(sdk_config(collector_endpoint="10.255.255.1:4317"), SpanBuffer(4))
    started = time.perf_counter()
    exporter.start()
    elapsed = time.perf_counter() - started
    try:
        assert elapsed < 0.5
    finally:
        exporter.shutdown(timeout=0.5)
