"""Fixtures for the SDK test suite.

The SDK has process-global state on purpose — one tracer, one buffer, one
exporter thread, and class-level hooks on SQLAlchemy and httpx. That makes
teardown the important part of every fixture here: a test that leaves the
tracer installed would silently instrument the sample-shop tests that run
after it, so `_assert_clean_teardown` fails any test that does.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager

import pytest

import aperture
from aperture.buffer import SpanBuffer
from aperture.config import ApertureConfig
from aperture.hooks import clear_code_location_cache
from aperture.hooks import pool as pool_hook
from aperture.spans import Tracer

# A port nothing is listening on. Used wherever a test does not care about
# export succeeding, which is most of them: the SDK must behave identically
# whether or not a collector exists.
DEAD_COLLECTOR = "127.0.0.1:14317"


def build_config(**overrides) -> ApertureConfig:
    """A config suitable for tests: enabled, and pointed at nothing.

    `export_interval_ms` is deliberately long so the exporter thread does not
    wake up and log connection failures during unrelated tests. A test that
    wants an export calls `aperture.flush()`.
    """
    defaults: dict = dict(
        enabled=True,
        service_name="test-service",
        service_version="9.9.9",
        environment="test",
        collector_endpoint=DEAD_COLLECTOR,
        export_interval_ms=60_000,
        shutdown_timeout_s=0.5,
        buffer_capacity=1024,
    )
    defaults.update(overrides)
    return ApertureConfig(**defaults)


@pytest.fixture
def dead_collector() -> str:
    return DEAD_COLLECTOR


@pytest.fixture
def sdk_config():
    """Factory for a test configuration."""
    return build_config


@pytest.fixture
def tracer_factory():
    """Build a Tracer + SpanBuffer pair without touching global state.

    Used by tests about the span model itself, which have no reason to install
    hooks or start an exporter thread.
    """

    def _make(**overrides) -> tuple[Tracer, SpanBuffer]:
        config = build_config(**overrides)
        buffer = SpanBuffer(config.buffer_capacity)
        return Tracer(config, buffer), buffer

    return _make


@pytest.fixture
def instrumented():
    """Install the SDK for one test and guarantee it is removed afterwards.

    Yields a callable so a test can choose its own configuration:

        config = instrumented(buffer_capacity=4)
    """

    def _install(**overrides) -> ApertureConfig:
        config = build_config(**overrides)
        aperture.instrument(config)
        return config

    try:
        yield _install
    finally:
        aperture.shutdown(timeout=0.5)
        clear_code_location_cache()
        pool_hook.reset_stats()


@pytest.fixture(autouse=True)
def _assert_clean_teardown() -> Iterator[None]:
    """Fail loudly if a test leaks instrumentation into the next one."""
    yield
    if aperture.is_enabled():
        aperture.shutdown(timeout=0.5)
        pytest.fail(
            "a test left Aperture instrumentation installed; every test that "
            "instruments must use the `instrumented` fixture so teardown runs"
        )


# ---------------------------------------------------------------------------
# A real OTLP receiver
# ---------------------------------------------------------------------------


class CollectingTraceService:
    """An OTLP/gRPC trace receiver that keeps what it is sent.

    This is a real server speaking the real protocol — the generated
    `TraceServiceServicer` from `opentelemetry-proto`, which is the same
    service definition the Go collector will implement on Week 1 Day 5. It
    stands in for the *collector*, not for anything in the SDK: the export
    path under test does genuine protobuf serialisation and gRPC over a real
    socket.
    """

    def __init__(self) -> None:
        self.requests: list = []
        self.fail_with = None  # set to a grpc.StatusCode to reject batches

    def Export(self, request, context):  # noqa: N802 - gRPC naming convention
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

        if self.fail_with is not None:
            context.abort(self.fail_with, "collector rejected the batch")
        self.requests.append(request)
        return trace_service_pb2.ExportTraceServiceResponse()

    @property
    def spans(self) -> list:
        return [
            span
            for request in self.requests
            for resource_spans in request.resource_spans
            for scope_spans in resource_spans.scope_spans
            for span in scope_spans.spans
        ]

    @property
    def resources(self) -> list:
        return [rs.resource for r in self.requests for rs in r.resource_spans]


@contextmanager
def otlp_receiver() -> Iterator[tuple[CollectingTraceService, str]]:
    """Run a real OTLP gRPC server on an ephemeral port."""
    import grpc
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    servicer = CollectingTraceService()
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield servicer, f"127.0.0.1:{port}"
    finally:
        server.stop(None)


@pytest.fixture
def collector() -> Iterator[tuple[CollectingTraceService, str]]:
    with otlp_receiver() as receiver:
        yield receiver
