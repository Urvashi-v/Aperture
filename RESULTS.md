# Results

**Nothing in this file is measured yet, because the thing that would be
measured does not exist yet.**

The analysis engine, the SDK, and the collector are all unbuilt as of Day 1.
There are therefore no detection results, no overhead numbers, and no
verification outcomes. This file exists now so that the shape of the experiment
is fixed *before* any detector is written — deciding what counts as success
after seeing the results is how evaluations become worthless.

Every number below is marked `UNMEASURED` and will be replaced by output from
`eval/run_evaluation.py`. No value in this file, in `README.md`, or in any
dashboard will ever be typed in by hand.

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
| SDK added p99 latency | < 1 ms (C1) | UNMEASURED |
| SDK CPU overhead | < 2% (C1) | UNMEASURED |
| SDK steady-state RSS | bounded (C3) | UNMEASURED |
| Ingest throughput | — | UNMEASURED |
| Analysis latency over a 1-hour window | — | UNMEASURED |
| Storage bytes per span after compression | — | UNMEASURED |

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

**Test suite.** 105 tests, all passing, ~4 s, against a real PostgreSQL.

**Smoke test.** 27/27 endpoint checks passing against a running server, at both
`small` and `medium`.

---

## Row-count honesty

The `large` profile *targets* 1.2M orders and 1.5M posts. **It has not been
run.** The largest dataset this repository has actually built is `medium`, at
2,012,141 rows, shown above. Extrapolating from the medium run, `large` should
take roughly nine minutes and several gigabytes, but that is arithmetic, not a
measurement.

`shop-seed --report` prints the real counts from whatever database you point it
at. No claim about dataset size should be made from anywhere else.
