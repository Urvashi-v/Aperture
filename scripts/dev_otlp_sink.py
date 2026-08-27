"""A development OTLP/gRPC sink that prints what the SDK sends it.

    python scripts/dev_otlp_sink.py                 # listen on 0.0.0.0:4317
    python scripts/dev_otlp_sink.py --port 4317 --tree

THIS IS NOT THE COLLECTOR. The collector is Week 1 Day 5: a Go binary that
does trace assembly, tail-based sampling and ClickHouse writes. This is fifty
lines of Python that accepts an OTLP export and prints it, so that the SDK can
be verified end to end before that exists. It stores nothing and analyses
nothing.

It is useful for exactly one question: "is the SDK actually putting correct,
complete spans on the wire?"
"""

from __future__ import annotations

import argparse
import signal
import threading
from collections import Counter
from concurrent import futures

import grpc
from opentelemetry.proto.collector.trace.v1 import (
    trace_service_pb2,
    trace_service_pb2_grpc,
)

KIND_NAMES = {0: "UNSET", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS_NAMES = {0: "unset", 1: "ok", 2: "ERROR"}


def attr_value(value) -> object:
    for field in ("string_value", "int_value", "double_value", "bool_value"):
        if value.HasField(field):
            return getattr(value, field)
    return ""


class PrintingTraceService(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, show_tree: bool, show_sql: bool) -> None:
        self.show_tree = show_tree
        self.show_sql = show_sql
        self.lock = threading.Lock()
        self.spans_received = 0
        self.batches = 0
        self.by_endpoint: Counter = Counter()
        self.by_operation: Counter = Counter()

    def Export(self, request, context):  # noqa: N802 - gRPC naming
        spans = []
        service = "?"
        for resource_spans in request.resource_spans:
            for kv in resource_spans.resource.attributes:
                if kv.key == "service.name":
                    service = attr_value(kv.value)
            for scope_spans in resource_spans.scope_spans:
                spans.extend(scope_spans.spans)

        with self.lock:
            self.batches += 1
            self.spans_received += len(spans)
            for span in spans:
                attributes = {kv.key: attr_value(kv.value) for kv in span.attributes}
                self.by_operation[span.name] += 1
                route = attributes.get("http.route")
                if route:
                    self.by_endpoint[route] += 1

            print(
                f"[batch {self.batches}] service={service} spans={len(spans)} "
                f"(total {self.spans_received})",
                flush=True,
            )
            if self.show_tree:
                self._print_spans(spans)

        return trace_service_pb2.ExportTraceServiceResponse()

    def _print_spans(self, spans) -> None:
        by_trace: dict[bytes, list] = {}
        for span in spans:
            by_trace.setdefault(span.trace_id, []).append(span)

        for trace_id, group in by_trace.items():
            print(f"  trace {trace_id.hex()}  ({len(group)} spans)")
            for span in sorted(group, key=lambda s: s.start_time_unix_nano):
                attributes = {kv.key: attr_value(kv.value) for kv in span.attributes}
                duration_ms = (span.end_time_unix_nano - span.start_time_unix_nano) / 1e6
                bits = [
                    f"    {KIND_NAMES.get(span.kind, '?'):<8}",
                    f"{span.name:<22}",
                    f"{duration_ms:8.2f}ms",
                    f"status={STATUS_NAMES.get(span.status.code, '?')}",
                ]
                if "aperture.db.rows" in attributes:
                    bits.append(f"rows={attributes['aperture.db.rows']}")
                if attributes.get("aperture.pool.wait_ns"):
                    bits.append(f"pool={int(attributes['aperture.pool.wait_ns']) / 1e6:.1f}ms")
                if "aperture.code.location" in attributes:
                    bits.append(f"at={attributes['aperture.code.location']}")
                print(" ".join(bits))
                if self.show_sql and "db.statement" in attributes:
                    print(f"             {str(attributes['db.statement'])[:110]}")

    def summary(self) -> None:
        print("\n" + "=" * 70)
        print(f"batches: {self.batches}   spans: {self.spans_received}")
        if self.by_endpoint:
            print("\nspans per endpoint")
            for endpoint, count in self.by_endpoint.most_common(20):
                print(f"  {count:>6}  {endpoint}")
        if self.by_operation:
            print("\nspans per operation")
            for operation, count in self.by_operation.most_common(20):
                print(f"  {count:>6}  {operation}")
        print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4317)
    parser.add_argument("--tree", action="store_true", help="print every span")
    parser.add_argument("--sql", action="store_true", help="print SQL statements")
    args = parser.parse_args()

    servicer = PrintingTraceService(show_tree=args.tree, show_sql=args.sql)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"{args.host}:{args.port}")
    server.start()
    print(f"OTLP/gRPC sink listening on {args.host}:{args.port}  (Ctrl-C to stop)")

    stop = threading.Event()

    def handle_signal(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        stop.wait()
    finally:
        servicer.summary()
        server.stop(1).wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
