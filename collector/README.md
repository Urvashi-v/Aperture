# `collector/` — OTLP ingest (Go)

**Status: not started.** Week 1, Day 5.

Single static Go binary. Terminates OTLP/gRPC, reassembles spans into complete
traces, applies tail-based sampling with full trace context, and writes to
ClickHouse.

Go rather than Python because this is the ingest hot path: goroutine
concurrency and low GC pressure suit it, and a performance tool whose own
collector is the bottleneck is not a good look. See
[`../DESIGN.md`](../DESIGN.md) §5.1.

Tail sampling needs a bounded per-trace buffer with timeout eviction, plus a
metric on evictions — an unbounded buffer on trace fragments is a named risk in
§10.
