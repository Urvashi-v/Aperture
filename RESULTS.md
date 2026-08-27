# Results

**No detection results exist, because no detector exists.**

As of Day 2 the benchmark application and the instrumentation SDK are working;
the collector, the analysis engine and the dashboard are unbuilt. There are
therefore no recall or precision figures, no verification outcomes, and no
validated overhead number.

The headline tables stay marked `UNMEASURED` and will be written by
`eval/run_evaluation.py`. They are fixed in shape *before* any detector is
written, because deciding what counts as success after seeing the results is
how evaluations become worthless. No value in this file, in `README.md`, or in
any dashboard will ever be typed in by hand.

Below the tables is a record of what *has* actually been measured on this
machine. Those are evidence that the planted pathologies and the trace pipeline
are real — they are not results, and each says so.

---

## Detection quality — UNMEASURED

| Metric | Target (DESIGN.md §8.2) | Measured |
|---|---|---|
| Planted pathologies | 8 | 5 planted so far, 3 pending |
| Recall | ≥ 7/8 | UNMEASURED |
| Precision | ≥ 0.90 | UNMEASURED |
| False positives on control endpoints | 0 | UNMEASURED |
| Control endpoints | 12 | 15 exist (see PATHOLOGIES.md) |
| Time to detection | — | UNMEASURED |
| Unplanted issues found | — | UNMEASURED |

## Recommendation quality — UNMEASURED

| Metric | Measured |
|---|---|
| Verification pass rate | UNMEASURED |
| Median p95 improvement on affected endpoints | UNMEASURED |
| Recommendations discarded by verification | UNMEASURED |

## System performance — UNMEASURED

| Metric | Constraint | Measured |
|---|---|---|
| SDK added p99 latency | < 1 ms (C1) | **UNMEASURED** (see note) |
| SDK CPU overhead | < 2% (C1) | UNMEASURED |
| SDK steady-state RSS | bounded (C3) | UNMEASURED |
| Ingest throughput | — | UNMEASURED |
| Analysis latency over a 1-hour window | — | UNMEASURED |
| Storage bytes per span after compression | — | UNMEASURED |

**Note on C1.** A preliminary in-process figure exists and is reported below,
but it is not a validation of C1 and must not be quoted as one. C1 is a claim
about a service under concurrent load measured from outside the process; the
Week 1 Day 7 milestone measures that with k6, and it has not been run. The
preliminary figure is **over budget**, which is a reason to investigate on
Day 7, not a result.

---

## What *has* been measured (Day 1)

These are real numbers from this machine. They are here because they are
evidence that the planted pathologies exist, not because they are results.

**Environment.** PostgreSQL 16.14 in Docker, single laptop, single client, no
concurrent load. Absolute timings are not meaningful as benchmarks; the plan
shapes and the pathological-vs-control ratios are.

**Seed runs, both actually executed.**

```
profile=small   seed=1337   duration=9.5s     185,171 rows
profile=medium  seed=1337   duration=109.2s 2,012,141 rows   535 MB on disk

medium breakdown:
  users             20,000        posts            500,000
  categories            12        follows          400,000
  products          60,000        reviews          300,000
  orders           250,000        order_items      482,129
```

Re-running `small` with the same seed produced byte-identical row counts, so
the dataset is reproducible.

**P3 evidence** (medium) — `SELECT count(*) FROM orders WHERE user_id = 7`:
`Parallel Seq Scan on orders`, ~249,648 rows removed by filter across three
workers, 29.9 ms. The paginated page query takes a different route —
`Index Scan Backward using idx_orders_placed_at` with 19,040 rows removed by
filter — which is a design constraint on detector D2 recorded in
PATHOLOGIES.md.

**P4 evidence** (medium) — the feed query: `Parallel Seq Scan on posts`,
~493,962 rows removed by filter, **25,339 pages read from disk to return 20
rows**, 74.9 ms.

**P7 evidence** — `GET /api/admin/export` with no window: 25,001 rows / 4.0 MB
/ ~620 ms at `small`; **250,001 rows / 6.3 s** at `medium`.

**P1 and P2 evidence** — query counts recorded by the test suite:
`GET /api/orders?limit=N` issues exactly N line-item queries; the product page
issues one author lookup per review rendered.

**Pathological vs control latency** (medium, 15 samples, median): P1 4.2×
slower than its control, P2 2.6×, P3 2.5×, P4 2.2×. Full table in
PATHOLOGIES.md.

**Test suite.** 276 tests, all passing, ~33 s. Against a real PostgreSQL, a
real OTLP/gRPC server, real sockets, and real connection-pool contention.

**Smoke test.** 27/27 endpoint checks passing against a running server, at both
`small` and `medium`, and again with the SDK enabled.

---

## Day 2 — the instrumentation SDK

Also real numbers from this machine, and also not results.

**End-to-end export.** With the SDK enabled and a real OTLP/gRPC receiver
listening, a 27-check smoke run produced **115 spans started, 115 finished, 115
buffered, 115 exported**; 0 export failures, 0 dropped for any reason, buffer
peak 31 of 8192.

**Fail-open, verified.** With nothing listening on the collector endpoint, all
27 smoke checks still pass and every span that did not make it out is accounted
for in `spans_dropped_export_failure`.

**Trace shape.** `GET /api/orders?limit=6` yields 9 spans: one SERVER, one auth
lookup, one orders query, and **six sibling DB spans sharing one fingerprint,
non-overlapping, one row each, from a single call site**
(`shop/routers/orders.py:95`). That is planted pathology P1 with every clause of
the DESIGN.md §6.2 signature present, which is the precondition detector D1
needs.

**SDK primitives** (micro-benchmark, in-process, idle machine):

| Operation | Cost |
|---|---:|
| span start + end, including buffer put | ~3.8 µs |
| buffer put | ~0.9 µs |
| placeholder fingerprint | ~1.4 µs |
| code-location capture | ~1.4 µs |

**Preliminary end-to-end overhead — NOT a C1 validation.** On
`GET /api/orders?limit=10` (about 13 queries, 15 spans per request), 5
interleaved rounds of 120 requests each:

```
baseline      p50 38.566 ms   p95 51.549 ms   p99 64.784 ms
instrumented  p50 41.299 ms   p95 54.727 ms   p99 72.659 ms
delta         p50 +2.733 ms (+7.1%)
noise floor   0.680 ms
```

The delta is outside the run's own noise floor, so it is real for this
workload on this machine — and it is well over the C1 budget. The per-primitive
costs above sum to roughly 100 µs per request, not 2.7 ms, so most of the cost
is somewhere the micro-benchmark does not look. Finding it is Day 7's job.

**Two bugs this measurement work found**, both recorded because they are the
kind that stay found only if written down:

1. **`time.time_ns()` has 15.625 ms resolution on Windows** — 20,000
   consecutive calls returned two distinct values. Span timestamps had to move
   to the monotonic clock, or every N+1 would have been indistinguishable from
   a concurrent fan-out.
2. **Fingerprints overflowed protobuf's signed `int64`** — 90 of 200 samples,
   and protobuf's failure took the entire export batch rather than the one
   span. Only visible against a real OTLP receiver.

**One non-bug worth recording:** an earlier version of the benchmark reported
+8.9 ms at p50, and two changes were investigated on the strength of it. A
properly interleaved re-run showed the delta was machine drift. The benchmark
now interleaves and computes its own noise floor, and says so when a result
falls inside it.

---

## Row-count honesty

The `large` profile *targets* 1.2M orders and 1.5M posts. **It has not been
run.** The largest dataset this repository has actually built is `medium`, at
2,012,141 rows, shown above. Extrapolating from the medium run, `large` should
take roughly nine minutes and several gigabytes, but that is arithmetic, not a
measurement.

`shop-seed --report` prints the real counts from whatever database you point it
at. No claim about dataset size should be made from anywhere else.
