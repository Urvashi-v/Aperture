# `sdk/` — in-process instrumentation

**Status: not started.** Week 1, Days 2–4.

Python package (`aperture`) that the target application installs and enables
with a single ASGI middleware. Design constraints C1–C3 in
[`../DESIGN.md`](../DESIGN.md) §7 govern everything here:

- `middleware.py` — ASGI server span, `contextvars` trace propagation
- `hooks/` — SQLAlchemy `before/after_cursor_execute`, pool checkout/checkin,
  `httpx` event hooks, cached code-location capture
- `buffer.py` — fixed-size ring buffer; on overflow, drop and count, never block
- `exporter.py` — background OTLP/gRPC batch export, fail-open
- `benchmarks/` — the overhead measurement, with its results committed

Nothing in `sample-shop` will import this package. Enabling it must be one
middleware registration and nothing else (C2).
