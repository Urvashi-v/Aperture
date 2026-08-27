# `sdk/` — in-process instrumentation

**Status: working.** Week 1, Days 2–4 (SDK), built ahead of the Go collector.

A Python package (`aperture`) that an ASGI application installs with one call.
It captures HTTP requests, SQL queries, connection-pool waits and outbound HTTP
as OpenTelemetry spans, and ships them to an OTLP/gRPC collector from a
background thread.

## Integration — the whole thing

```python
from aperture import instrument_app

app = FastAPI()
instrument_app(app, service_name="sample-shop", service_version="1.4.2")
```

That is the complete integration surface, and it is design constraint C2:
**zero application code changes beyond one middleware.** The application never
hands over its engine, never wraps a session, and never touches a request
handler. Queries, pool waits and outbound HTTP are picked up by hooks installed
on the SQLAlchemy and httpx *classes*, so an engine created later — by a test
fixture, a migration, a second database — is instrumented too.

Nothing happens unless `APERTURE_SDK_ENABLED=true`. When it is off,
`instrument_app` returns the application untouched and does not add middleware
at all, so a disabled SDK costs the request path nothing — not even one extra
`await`.

## The three properties this SDK will not trade away

**The request path never does network I/O.** A finished span is appended to a
fixed-size ring buffer and the request moves on. A daemon thread drains the
ring on an interval and does the gRPC. A thread rather than an asyncio task,
because an asyncio exporter shares the event loop with request handlers and
would compete with real work for loop time.

**Memory is bounded.** The ring buffer, the code-location cache, the
per-filename classification cache and the per-trace span budget all have
ceilings. There is no retry queue, because a queue of un-exported batches that
grows while the collector is down is precisely the unbounded-memory failure C3
forbids. Failed batches are dropped and counted.

**It fails open.** Collector down, malformed environment variable, bug in a
hook — the application continues. Every hook callback is wrapped so it cannot
raise into the host, and a failure to connect is logged once per transition
into failure rather than once per attempt.

Verified end to end: with nothing listening on the collector endpoint, 27/27
smoke checks pass against sample-shop and the drop counters account for every
span that did not make it out.

## Layout

| File | Responsibility |
|---|---|
| `aperture/__init__.py` | `instrument_app`, `instrument`, `shutdown`, `get_stats` |
| `aperture/config.py` | `APERTURE_*` environment parsing. Defensive: a malformed value logs and falls back, never raises |
| `aperture/context.py` | `contextvars` propagation, W3C `traceparent` parse/format |
| `aperture/spans.py` | The `Span` model, `Tracer`, the clock |
| `aperture/buffer.py` | The bounded ring, and the drop counters |
| `aperture/exporter.py` | OTLP protobuf encoding, gRPC transport, backoff |
| `aperture/middleware.py` | Raw ASGI middleware; the SERVER span |
| `aperture/hooks/sqlalchemy.py` | One CLIENT span per statement |
| `aperture/hooks/pool.py` | Measured connection-acquisition wait |
| `aperture/hooks/httpx.py` | Outbound CLIENT spans, `traceparent` injection |
| `benchmarks/overhead.py` | Overhead measurement — see `benchmarks/README.md` |

## What a span carries

Exactly the columns DESIGN.md §5.2 stores in ClickHouse: trace/span/parent ids,
service, operation, kind, start time, duration, status, endpoint, DB statement,
DB fingerprint, DB row count, pool wait, code location, and free-form
attributes.

The wire contract with the Go collector is the attribute key list at the top of
`exporter.py`. Where OpenTelemetry has a stable semantic convention we use it
(`db.statement`, `http.route`), so a stock OTel collector can also read the
data; everything else is namespaced under `aperture.`.

## Things worth knowing

### Timestamps come from the monotonic clock, not `time.time_ns()`

On Windows `time.time_ns()` has **15.625 ms** resolution — measured here,
20,000 consecutive calls returned two distinct values. Detector D1's
non-overlap guard and the whole of D4 are statements about whether spans
overlap in time. At 15 ms granularity, twenty sibling queries taking 2 ms each
all carry the same timestamp and an N+1 becomes indistinguishable from a
deliberate concurrent fan-out.

Span times are therefore derived from `perf_counter_ns` against a single
wall-clock anchor. This costs one clock read per span instead of two, and the
tradeoff is explicit: absolute timestamps drift from the system clock at
whatever rate the two oscillators differ, while ordering and durations *within*
a trace are exact. Trace analysis only ever asks the second kind of question.

### Capturing the call site requires crossing a greenlet boundary

SQLAlchemy's asyncio support runs the DBAPI layer inside a greenlet, which has
its own stack. Walking `f_back` from inside `before_cursor_execute` reaches
SQLAlchemy's own frames and then simply ends — measured here at seven frames.
The application code that issued the query is on the *parent greenlet's* stack
and is invisible to a normal walk.

`hooks/__init__.py` continues the walk from
`greenlet.getcurrent().parent.gr_frame`, which reaches the real call site. This
is the difference between every finding saying `site: shop/routers/orders.py:95`
and every finding saying `site:`.

### The code-location cache is off by default — a deviation from the design

DESIGN.md §7.3 says to memoise the call site per fingerprint because stack
capture is expensive. That is true of `traceback.extract_stack`, which reads
source files off disk. It is not true of the frame walk used here, which reads
two attributes per frame: **measured at 1.4 µs against a 2.2 ms query, 0.06%**.

What the cache costs when it is on is correctness on the case that matters
most. Two call sites issuing byte-identical SQL share one entry, and the second
inherits the first's location. In sample-shop the auth dependency and the
product-page N+1 both run `SELECT ... FROM users WHERE id = $1` — with the
cache on, the N+1 finding points at the auth dependency. Set
`APERTURE_CODE_LOCATION_CACHE=true` to restore the design's behaviour, and read
locations as "where this SQL was first seen" when you do.

### Bound parameters are hashed, never stored

D1 has to tell "the same query with varying parameters" (an N+1) from "the same
query with identical parameters" (a caching problem). That needs parameter
*variance*, not parameter *values*. Each span carries a digest; the values
never leave the hook. Query strings on HTTP spans are dropped for the same
reason — only the path is kept, and the server span records the query string's
size rather than its content.

### Fingerprints are 63-bit, not 64-bit

OTLP carries integer attributes in a protobuf `int64`, which is signed. A full
64-bit hash overflows it about half the time, and protobuf does not fail
politely — it raises while serialising, taking the whole batch with it. This
was found by pointing the SDK at a real collector: 90 of 200 sample
fingerprints were out of range, and several thousand good spans were being
dropped because of it.

Masking to 63 bits produces a value that is simultaneously a valid positive
`int64` and a valid ClickHouse `UInt64`, so nothing downstream has to
reinterpret two's-complement bit patterns. The encoder also encodes span by
span now, so a value it cannot serialise costs one span rather than a batch.

## Diagnostics

Set `APERTURE_STATS_PATH=/aperture/stats` and the middleware answers that exact
path with the SDK's own counters, including every drop counter. "How many spans
did you throw away, and why" is the question an operator actually needs
answered, and a telemetry library that cannot answer it is asking to be trusted
on faith.

```
spans_started 115   spans_exported 115   export_failures 0
spans_dropped_buffer_full 0   spans_dropped_export_failure 0
buffer_peak_size 31   buffer_capacity 8192
```

`aperture.get_stats()` returns the same dictionary in-process.

## Verifying it without a collector

The Go collector arrives on Week 1 Day 5. Until then:

```bash
python scripts/dev_otlp_sink.py --tree --sql
```

That is a development stand-in — a real OTLP/gRPC server that prints what it
receives and stores nothing. It is not the collector and does no assembly,
sampling or storage.

## Not done yet

- **No span object pool.** DESIGN.md §7.1.1 calls for pre-allocated span
  structs. Spans use `slots=True`, which is the cheap and safe half; recycling
  objects would save an allocation and introduce use-after-free bugs. Day 7
  measures overhead — if allocation shows up there, a pool can go behind this
  same interface. Optimising before the measurement that exists to guide the
  optimisation is the wrong order.
- **Overhead is not validated against C1.** See `benchmarks/README.md`.
- **The fingerprint is a placeholder.** It hashes normalised statement *text*,
  so `WHERE id = 42` and `WHERE id = 77` do not collapse to one identity. The
  real one (sqlglot, DESIGN.md §6.1) is Week 2 Day 9. Every span records
  `db_fingerprint_method` so nothing downstream can mistake one for the other.
- **The HTTP URL template is a placeholder too**, a path-segment heuristic that
  collapses numeric and hex segments. It does not handle slugs.
