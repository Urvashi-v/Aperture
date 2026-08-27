"""OTLP/gRPC export, on a background thread.

Nothing in this module is ever called from the request path. The request path
calls `SpanBuffer.put` and returns; a daemon thread drains the ring on an
interval and does the network I/O. That separation is design rule 7.1.2, and
it is the reason a collector outage cannot slow the host application down.

**Why a thread rather than an asyncio task.** An asyncio exporter would share
the event loop with the application's request handlers, so a slow collector
would compete with real work for loop time even though it never blocks. A
daemon thread has no such coupling, and CPython releases the GIL for the
duration of a socket send, so the export genuinely runs in parallel with
request handling.

**Failure behaviour.** Any export failure is counted and the batch is dropped.
There is no retry queue, because a retry queue that grows while the collector
is down is exactly the unbounded-memory failure C3 forbids. After a few
consecutive failures the exporter backs off, so a collector that is down costs
the host one connection attempt every 30 seconds rather than one per interval
forever.
"""

from __future__ import annotations

import logging
import threading
import time

from opentelemetry.proto.collector.trace.v1 import (
    trace_service_pb2,
    trace_service_pb2_grpc,
)
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from aperture.buffer import SpanBuffer
from aperture.config import ApertureConfig
from aperture.spans import UNKNOWN_ROWS, Span

logger = logging.getLogger("aperture.exporter")

INSTRUMENTATION_SCOPE = "aperture-sdk"
INSTRUMENTATION_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Attribute contract.
#
# These keys are the interface between this SDK and the Go collector (Week 1
# Day 5), which decodes them into the ClickHouse columns in DESIGN.md 5.2.
# Changing one of them is a breaking change on both sides.
#
# Where OpenTelemetry has a stable semantic convention we use it, so that a
# stock OTel collector can also read our data. Anything without one is
# namespaced under `aperture.`.
# ---------------------------------------------------------------------------
ATTR_HTTP_ROUTE = "http.route"                       # -> endpoint
ATTR_DB_STATEMENT = "db.statement"                   # -> db_statement
ATTR_DB_FINGERPRINT = "aperture.db.fingerprint"      # -> db_fingerprint
ATTR_DB_FINGERPRINT_METHOD = "aperture.db.fingerprint_method"
ATTR_DB_ROWS = "aperture.db.rows"                    # -> db_rows
ATTR_POOL_WAIT_NS = "aperture.pool.wait_ns"          # -> pool_wait_ns
ATTR_CODE_LOCATION = "aperture.code.location"        # -> code_location

# Spans this SDK could not serialise. Process-wide, because encoding happens
# in a module-level function shared by every exporter instance.
spans_dropped_encode_error = 0


# protobuf `int64` bounds. OTLP has no unsigned integer attribute type.
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _int_attr(key: str, value: int) -> common_pb2.KeyValue:
    """An integer attribute, or a string one if it will not fit in an int64.

    protobuf raises `ValueError: Value out of range` while serialising an
    out-of-range int64, and it does so for the whole message — one oversized
    attribute would otherwise discard every span in the batch. Producers here
    are already masked to 63 bits, so this is a backstop rather than the
    primary defence, but it is the difference between losing one field and
    losing a thousand spans.
    """
    if _INT64_MIN <= value <= _INT64_MAX:
        return common_pb2.KeyValue(key=key, value=common_pb2.AnyValue(int_value=value))
    return common_pb2.KeyValue(
        key=key, value=common_pb2.AnyValue(string_value=str(value))
    )


def _str_attr(key: str, value: str) -> common_pb2.KeyValue:
    return common_pb2.KeyValue(
        key=key, value=common_pb2.AnyValue(string_value=value)
    )


def encode_span(span: Span) -> trace_pb2.Span:
    """Convert one `Span` to its OTLP protobuf form.

    The int-to-bytes conversions happen here, on the exporter thread, which is
    why `Span` stores identifiers as integers in the first place.
    """
    attributes: list[common_pb2.KeyValue] = []

    if span.endpoint:
        attributes.append(_str_attr(ATTR_HTTP_ROUTE, span.endpoint))
    if span.db_statement:
        attributes.append(_str_attr(ATTR_DB_STATEMENT, span.db_statement))
    if span.db_fingerprint:
        attributes.append(_int_attr(ATTR_DB_FINGERPRINT, span.db_fingerprint))
        attributes.append(
            _str_attr(ATTR_DB_FINGERPRINT_METHOD, span.db_fingerprint_method)
        )
    if span.db_rows != UNKNOWN_ROWS:
        attributes.append(_int_attr(ATTR_DB_ROWS, span.db_rows))
    if span.pool_wait_ns:
        attributes.append(_int_attr(ATTR_POOL_WAIT_NS, span.pool_wait_ns))
    if span.code_location:
        attributes.append(_str_attr(ATTR_CODE_LOCATION, span.code_location))

    for key, value in span.attributes.items():
        attributes.append(_str_attr(key, value))

    return trace_pb2.Span(
        trace_id=span.trace_id.to_bytes(16, "big"),
        span_id=span.span_id.to_bytes(8, "big"),
        # OTLP wants an empty field, not eight zero bytes, for a root span.
        parent_span_id=(
            span.parent_span_id.to_bytes(8, "big") if span.parent_span_id else b""
        ),
        name=span.operation,
        kind=int(span.kind),
        start_time_unix_nano=span.start_unix_ns,
        end_time_unix_nano=span.end_unix_ns,
        attributes=attributes,
        status=trace_pb2.Status(
            code=int(span.status),
            message=span.status_message,
        ),
    )


def encode_batch(
    config: ApertureConfig, spans: list[Span]
) -> trace_service_pb2.ExportTraceServiceRequest:
    """Wrap a batch of spans in a complete OTLP export request."""
    resource = resource_pb2.Resource(
        attributes=[
            _str_attr("service.name", config.service_name),
            _str_attr("service.version", config.service_version),
            _str_attr("deployment.environment", config.environment),
            _str_attr("telemetry.sdk.name", INSTRUMENTATION_SCOPE),
            _str_attr("telemetry.sdk.language", "python"),
            _str_attr("telemetry.sdk.version", INSTRUMENTATION_VERSION),
        ]
    )
    # Encoded one span at a time so that a span this SDK somehow cannot
    # serialise costs exactly that span. Encoding the list in a comprehension
    # meant one bad value took the whole batch with it, which is how a single
    # oversized fingerprint managed to drop several thousand good spans.
    encoded: list[trace_pb2.Span] = []
    for span in spans:
        try:
            encoded.append(encode_span(span))
        except Exception as exc:
            global spans_dropped_encode_error
            spans_dropped_encode_error += 1
            logger.debug("aperture: could not encode span %r: %s", span.operation, exc)

    scope_spans = trace_pb2.ScopeSpans(
        scope=common_pb2.InstrumentationScope(
            name=INSTRUMENTATION_SCOPE, version=INSTRUMENTATION_VERSION
        ),
        spans=encoded,
    )
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(resource=resource, scope_spans=[scope_spans])
        ]
    )


class OtlpSpanExporter:
    """Drains a `SpanBuffer` into an OTLP/gRPC collector."""

    def __init__(self, config: ApertureConfig, buffer: SpanBuffer) -> None:
        self._config = config
        self._buffer = buffer

        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._exporting = False

        self._channel = None
        self._stub = None
        self._channel_lock = threading.Lock()

        self.spans_exported = 0
        self.export_batches = 0
        self.export_failures = 0
        self.consecutive_failures = 0
        self.spans_dropped_export_failure = 0
        self.last_error: str = ""

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        # Daemon on purpose: a wedged exporter must never be the reason a
        # process refuses to exit.
        self._thread = threading.Thread(
            target=self._run, name="aperture-exporter", daemon=True
        )
        self._thread.start()
        logger.debug(
            "aperture exporter started", extra={"endpoint": self._config.collector_endpoint}
        )

    def shutdown(self, timeout: float | None = None) -> bool:
        """Stop the thread after one last attempt to drain the buffer."""
        if self._thread is None:
            return True
        budget = self._config.shutdown_timeout_s if timeout is None else timeout
        deadline = time.monotonic() + budget

        self._stopping.set()
        self._wake.set()
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        drained = len(self._buffer) == 0

        # One synchronous final attempt, so a clean shutdown does not throw
        # away spans that were already recorded.
        if not drained and time.monotonic() < deadline:
            self._export_pending()
            drained = len(self._buffer) == 0

        self._close_channel()
        self._thread = None
        return drained

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until the buffer is empty, or the timeout expires.

        For tests and for shutdown. Never call this from a request handler:
        it waits on network I/O, which is the one thing the request path must
        not do.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self._buffer) == 0 and not self._exporting:
                return True
            self._wake.set()
            time.sleep(0.005)
        return len(self._buffer) == 0 and not self._exporting

    # -- the background loop ------------------------------------------------

    def _run(self) -> None:
        interval = self._config.export_interval_ms / 1000.0
        while not self._stopping.is_set():
            wait_for = interval
            if self.consecutive_failures >= self._config.export_failure_backoff_after:
                wait_for = self._backoff_seconds()
            self._wake.wait(wait_for)
            self._wake.clear()
            try:
                self._export_pending()
            except Exception as exc:  # pragma: no cover - belt and braces
                logger.debug("aperture exporter cycle failed: %s", exc)

        # Final drain on the way out.
        try:
            self._export_pending()
        except Exception:  # pragma: no cover
            pass

    def _backoff_seconds(self) -> float:
        extra = self.consecutive_failures - self._config.export_failure_backoff_after
        base = self._config.export_interval_ms / 1000.0
        return min(base * (2 ** min(extra, 10)), self._config.export_backoff_max_s)

    def _export_pending(self) -> None:
        batch_size = self._config.export_batch_size
        self._exporting = True
        try:
            while True:
                batch = self._buffer.drain(batch_size)
                if not batch:
                    return
                self._export(batch)
                # If the collector is failing, stop draining. Continuing would
                # convert the whole buffer into dropped spans in one cycle.
                if self.consecutive_failures:
                    return
        finally:
            self._exporting = False

    def _export(self, batch: list[Span]) -> None:
        self.export_batches += 1
        try:
            stub = self._get_stub()
            request = encode_batch(self._config, batch)
            stub.Export(request, timeout=self._config.export_timeout_s)
        except Exception as exc:
            self.export_failures += 1
            self.consecutive_failures += 1
            self.spans_dropped_export_failure += len(batch)
            self.last_error = f"{type(exc).__name__}: {exc}"[:400]
            # Logged once per transition into failure, not once per batch: a
            # down collector must not also produce a flood of log lines.
            if self.consecutive_failures == 1:
                logger.warning(
                    "aperture: span export failed, dropping spans and backing off "
                    "(endpoint=%s): %s",
                    self._config.collector_endpoint,
                    self.last_error,
                )
            # The channel is deliberately NOT torn down here.
            #
            # A gRPC channel reconnects on its own, with its own backoff and
            # its own DNS re-resolution. Closing it after every failed export
            # threw that machinery away and paid to rebuild the channel - and
            # its C-core polling threads - on every retry, which is pure waste
            # in the exact situation where the process is already unhappy.
            #
            # Honest note on evidence: this was changed while chasing an
            # apparent p50 regression that a properly interleaved re-run showed
            # to be measurement drift, not a real effect. The change stands on
            # the reasoning above, not on that measurement. Whether channel
            # churn is visible at all is a question for the Day 7 benchmark.
            return

        if self.consecutive_failures:
            logger.info(
                "aperture: span export recovered after %d failures",
                self.consecutive_failures,
            )
        self.consecutive_failures = 0
        self.spans_exported += len(batch)

    # -- channel ------------------------------------------------------------

    def _get_stub(self):  # noqa: ANN201 - grpc stubs are untyped
        with self._channel_lock:
            if self._stub is None:
                import grpc

                target = self._config.collector_endpoint
                # grpc channels connect lazily, so building one here does no
                # network I/O and cannot block.
                if self._config.collector_insecure:
                    self._channel = grpc.insecure_channel(target)
                else:
                    self._channel = grpc.secure_channel(
                        target, grpc.ssl_channel_credentials()
                    )
                self._stub = trace_service_pb2_grpc.TraceServiceStub(self._channel)
            return self._stub

    def _close_channel(self) -> None:
        with self._channel_lock:
            channel = self._channel
            self._channel = None
            self._stub = None
        if channel is not None:
            try:
                channel.close()
            except Exception:  # pragma: no cover
                pass

    # -- introspection ------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> dict[str, object]:
        return {
            "exporter_running": self.running,
            "collector_endpoint": self._config.collector_endpoint,
            "spans_exported": self.spans_exported,
            "export_batches": self.export_batches,
            "export_failures": self.export_failures,
            "export_consecutive_failures": self.consecutive_failures,
            "spans_dropped_export_failure": self.spans_dropped_export_failure,
            "spans_dropped_encode_error": spans_dropped_encode_error,
            "last_export_error": self.last_error,
        }
