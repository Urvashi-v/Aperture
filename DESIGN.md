# Aperture — Design Document

*An automated performance pathology detector for backend APIs · v1.0*

This is the architectural source of truth for the project. Implementation
follows this document; where the implementation deviates, the deviation is
recorded in [§15](#15-deviations-from-v10) rather than silently applied.

---

## 0. One-line pitch

Existing observability tools tell you that your endpoint is slow. Aperture
tells you *why*, names the specific anti-pattern, and proves the fix works
before you ship it.

---

## 1. The problem

### 1.1 The situation on the ground

A backend team ships a feature. It is fine in dev. Two weeks later
`/api/users/feed` has a p95 of 1.4 seconds and the on-call engineer is staring
at a dashboard that says, accurately and uselessly, that `/api/users/feed` has
a p95 of 1.4 seconds.

What follows is a ritual: guess which part is slow, add ad-hoc logging,
redeploy, wait for traffic, read the logs, guess again — typically three to six
times, over hours to days. The engineer's skill is spent *locating* the
problem, not fixing it, and the location is almost always one of a small set of
recurring, well-understood, mechanically detectable anti-patterns.

### 1.2 The key insight

Performance bugs are not a long tail. They are a short, repetitive list.

| Pathology | Typical cause | Mechanically detectable? |
|---|---|---|
| N+1 queries | ORM lazy-loading inside a loop | Yes — structurally |
| Missing index | Query filters/joins on an unindexed column | Yes — via query plan |
| Connection pool saturation | Pool smaller than concurrency | Yes — wait-time vs exec-time |
| Serial external calls | Sequential awaits that could be concurrent | Yes — span overlap analysis |
| Unbounded result sets | Missing LIMIT / pagination | Yes — row count distribution |
| Cache stampede | Simultaneous misses on a hot key | Yes — miss clustering |
| Chatty microservice calls | Fan-out per item instead of batch | Yes — same shape as N+1 |

Every one of these has a structural signature in a distributed trace. Not a
statistical smell — an actual, deterministic, detectable shape.

**The central bet:** if the pathologies are finite and structurally distinct,
diagnosis can be automated. Nobody has to be clever. The tool just has to look.

---

## 2. Competitive analysis — the "how is this not Datadog?" question

### 2.1 The landscape

| Tool | What it does well | Where it stops |
|---|---|---|
| Datadog APM / New Relic / Dynatrace | Best-in-class collection, storage, visualisation at massive scale. Flame graphs, service maps, anomaly alerts. | Presents evidence, expects a human to diagnose. Surfaces "this span is slow"; does not say "this is an N+1 caused by lazy-loading `user.posts`". No verification of fixes. |
| Jaeger / Tempo / Zipkin | Open-source distributed tracing; storage and lookup. | Pure viewers. Zero analysis. |
| Prometheus + Grafana | Metrics, alerting, dashboards. | Aggregate metrics only — no causal structure, cannot see inside a request. |
| pganalyze / Percona PMM | Deep, genuinely excellent DB-side analysis. Index advice, plan inspection. | DB-blind to application context. Sees a flood of identical queries but cannot know they came from one HTTP request in a loop. |
| Django Silk / Laravel Telescope / rack-mini-profiler | Do detect N+1 in-framework. | Dev-only. Massive overhead, single-process, no production traffic, no aggregation over time. |
| Sentry Performance | Has shipped some N+1 detection. Closest commercial analogue. | Heuristic and shallow; no query-plan reasoning, no fix verification, no closed loop. |
| OpenTelemetry | The standard for instrumentation and wire format. | Deliberately a collection standard. Explicitly not an analysis layer. |

### 2.2 The actual gap

```
                    PRODUCTION-CAPABLE
                            ▲
                            │
     Datadog, New Relic  ●  │  ● pganalyze
     Jaeger, Grafana        │    (DB-only)
                            │
        ────────────────────┼────────────────────▶
        SHOWS EVIDENCE      │      DIAGNOSES CAUSE
                            │
                            │  ● ← APERTURE TARGETS HERE
     Django Silk         ●  │
     Laravel Telescope      │
                            │
                       DEV-ONLY
```

The upper-right quadrant — production-capable automated diagnosis with
application context — is genuinely thin.

### 2.3 The positioning statement

> Collection is a solved problem, so I use the OpenTelemetry standard for it
> rather than reinventing it. My contribution is the analysis engine: a set of
> structural detectors that consume OTLP traces and identify specific named
> anti-patterns, plus a verification loop that empirically confirms a proposed
> fix before a human acts on it. Datadog shows you a flame graph. Aperture says
> "this is an N+1 on `posts.author_id`, here is the fix, and here is the
> measured result of applying it in shadow."

### 2.4 What we are explicitly NOT claiming

- Not competing on scale, storage cost, or UI polish.
- Not a general-purpose monitoring platform.
- Not attempting infra-level metrics (that is Prometheus's job — we integrate,
  not replace).
- Not attempting to detect arbitrary performance problems. We detect a
  specific, enumerated, documented set.

---

## 3. Problem statement (formal)

**Given:** a stream of distributed traces from a running backend service, plus
read-only access to the database schema and query planner.

**Produce:** a ranked list of diagnosed performance pathologies, each with a
specific named anti-pattern classification, the exact code location and/or
query fingerprint responsible, quantified impact across the observed window, a
concrete proposed remediation, and — where safely possible — empirical
verification that the remediation works.

**Subject to constraints:**

| | |
|---|---|
| **C1** | Instrumentation overhead ≤ 2% CPU, ≤ 1 ms added p99 latency |
| **C2** | Zero application code changes beyond adding one middleware |
| **C3** | Bounded memory in the SDK regardless of traffic volume — never take down the host app |
| **C4** | Analysis must be specific, never "72% of time was in the DB, maybe try an index" |
| **C5** | No writes to production data. Verification runs against a shadow copy only |

---

## 4. Architecture

### 4.1 System overview

```
┌────────────────────────────────────────────────────────────┐
│ INSTRUMENTED APPLICATION                                    │
│                                                             │
│  HTTP middleware ─┐                                         │
│  DB driver hook  ─┼─▶ Span buffer (ring, bounded)           │
│  HTTP client hook─┘         │                               │
│                             ▼                               │
│                    Async batch exporter ──────────────┐     │
└───────────────────────────────────────────────────────┼─────┘
                                                        │ OTLP/gRPC
                                                        ▼
┌────────────────────────────────────────────────────────────┐
│ COLLECTOR                                                   │
│  OTLP ingest → trace assembly → tail sampling → write       │
└───────────────────────────────┬────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────┐
│ STORAGE — ClickHouse                                        │
│  spans (raw) · traces (assembled) · fingerprints · findings │
└───────────────────────────────┬────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────┐
│ ANALYSIS ENGINE          ← the actual project               │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ Structural   │  │ Plan-based   │  │ Temporal     │      │
│  │ detectors    │  │ detectors    │  │ detectors    │      │
│  │              │  │              │  │              │      │
│  │ N+1          │  │ missing idx  │  │ regression   │      │
│  │ serial await │  │ seq scan     │  │ change point │      │
│  │ chatty RPC   │  │ bad join     │  │ seasonality  │      │
│  │ pool starve  │  │ row estimate │  │              │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                            │                                │
│                            ▼                                │
│                   Finding ranker (impact × confidence)      │
│                            │                                │
│                            ▼                                │
│                   VERIFICATION SANDBOX                      │
│                   shadow DB → apply fix → re-measure        │
└───────────────────────────────┬────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────┐
│ API + DASHBOARD                                             │
└────────────────────────────────────────────────────────────┘
```

### 4.2 Layer responsibilities

- **SDK (in-process).** Capture spans with near-zero cost. Never block the
  request path. Never grow without bound. Degrade silently if the collector is
  unreachable.
- **Collector.** Terminate OTLP, reassemble spans into complete traces, make
  sampling decisions with full trace context, write to storage.
- **Storage.** Columnar, because every analytical query is "scan a lot of rows,
  aggregate on a few columns."
- **Analysis engine.** The project. Everything above exists to feed this.
- **Verification.** Turns a recommendation into a result.

---

## 5. Technology choices

### 5.1 Reference stack

| Layer | Choice | Why | Rejected alternative |
|---|---|---|---|
| Target app / SDK | Python — FastAPI + SQLAlchemy | ORM lazy-loading makes N+1 natural to plant; asyncio makes serial-await detection meaningful; SQLAlchemy has clean event hooks (`before_cursor_execute`) | Node/Express — fine, but Python's ORM ecosystem produces richer pathologies |
| Collector | Go | Concurrency and low GC pressure suit an ingest hot path; single static binary | Python — would bottleneck at ingest and is a bad look for a perf tool |
| Wire format | OTLP over gRPC | Industry standard; real OTel SDKs could feed us; avoids "reinvented tracing" criticism | Custom protocol — actively harmful to credibility |
| Storage | ClickHouse | Columnar, built for this access pattern; 10–20× compression on span data; sub-second aggregation over 100M+ rows | Postgres — degrades badly at scale; Elasticsearch — heavy, worse at numeric aggregation |
| Analysis engine | Python | Analysis is batch, not hot-path. `sqlglot`, `scipy`, `ruptures` are decisive | Go — would mean hand-writing a SQL parser |
| SQL parsing | `sqlglot` | Dialect-aware AST parsing; enables proper fingerprinting and column extraction | Regex — fails on subqueries, CTEs, nested parens. Do not do this. |
| Shadow DB | Postgres in Docker + `pg_stat_statements` | Real `EXPLAIN (ANALYZE, BUFFERS)`; schema restorable from dump | SQLite — planner too different to be meaningful |
| Dashboard | *see §15 — deviation* | | |
| Load generation | k6 | Scriptable scenarios, realistic arrival distributions, burst modelling | Locust — Python GIL limits generated load |

### 5.2 Why ClickHouse specifically

The workload is unambiguous. Writes are high-volume, append-only, batched,
never updated. Reads are
`SELECT quantile(0.95)(duration) FROM spans WHERE endpoint=? AND ts > ? GROUP BY fingerprint`
— few columns, many rows. That is the textbook columnar OLAP profile.

A `MergeTree` partitioned by day and ordered by `(service, endpoint, timestamp)`
scans only the relevant granules, and span data compresses extremely well
because adjacent rows share values.

```sql
CREATE TABLE spans (
    trace_id        FixedString(16),
    span_id         FixedString(8),
    parent_span_id  FixedString(8),
    service         LowCardinality(String),
    operation       String,
    kind            Enum8('server'=1,'client'=2,'internal'=3),
    start_ns        DateTime64(9),
    duration_ns     UInt64,
    status          Enum8('ok'=1,'error'=2),
    endpoint        LowCardinality(String),
    db_statement    String,
    db_fingerprint  UInt64,          -- hash of normalized SQL
    db_rows         UInt32,
    pool_wait_ns    UInt64,
    code_location   String,          -- file:line of the call site
    attributes      Map(String,String)
)
ENGINE = MergeTree
PARTITION BY toDate(start_ns)
ORDER BY (service, endpoint, start_ns)
TTL toDate(start_ns) + INTERVAL 30 DAY;
```

`LowCardinality` on the service/endpoint columns is dictionary encoding, and
these columns have a few hundred distinct values across millions of rows —
that is what makes the aggregations fast.

---

## 6. The analysis engine — core algorithms

This section is the intellectual content of the project.

### 6.1 SQL fingerprinting (prerequisite for everything)

Queries must be normalised so structurally identical queries with different
literals collapse to one identity.

```
Input:   SELECT * FROM posts WHERE author_id = 42 AND status = 'published'
Input:   SELECT * FROM posts WHERE author_id = 77 AND status = 'draft'
Output:  SELECT * FROM posts WHERE author_id = ? AND status = ?
         fingerprint = xxhash64(normalized) = 0x8f3a2b...
```

**Method:** parse to AST with `sqlglot`, walk the tree, replace every `Literal`
node with a placeholder, collapse `IN (?, ?, ?)` to `IN (?)`, normalise
whitespace and keyword case, re-serialise, hash.

**Why AST not regex:** regex breaks on string literals containing `?`, on
nested subqueries, on CTEs, on parenthesised expressions. A wrong fingerprint
silently destroys every downstream detector.

### 6.2 Detector 1 — N+1 queries (the flagship)

**The structural signature.** Within a single trace, under a common ancestor
span, there exist N ≥ threshold sibling DB spans sharing one fingerprint,
executing sequentially (non-overlapping in time), each returning few rows, with
varying bound parameters.

Every clause in that definition is doing work — dropping any one produces false
positives.

```
FOR each trace T:
    tree ← assemble_span_tree(T)          # parent_span_id → children
    FOR each non-leaf node P in tree:
        db_children ← [c for c in descendants(P) if c.kind == 'client:db']
        groups ← group_by(db_children, key=db_fingerprint)
        FOR (fingerprint, spans) in groups:
            IF len(spans) < N_MIN:            continue      # default 5
            IF overlapping(spans):            continue      # concurrent = intentional batch
            IF distinct_params(spans) == 1:   continue      # identical repeat = cache issue
            IF median_rows(spans) > ROW_MAX:  continue      # default 10; big results = pagination
            total     = sum(s.duration for s in spans)
            IF total / P.duration < 0.20:     continue      # not material, don't cry wolf
            EMIT Finding(
                type       = "N_PLUS_ONE",
                confidence = score(len(spans), total/P.duration, param_variance),
                parent     = P.operation,
                site       = spans[0].code_location,
                count      = len(spans),
                cost_ms    = total,
                fix        = suggest_eager_load(fingerprint, tree)
            )
```

| Guard | Prevents |
|---|---|
| Non-overlap check | Flagging a deliberate concurrent fan-out (`asyncio.gather` over 20 queries) |
| Param-variance check | Confusing N+1 with a caching problem (same query, same args, repeatedly) |
| Row-count ceiling | Flagging legitimate cursor-based pagination |
| Materiality threshold | Alert fatigue — 6 fast queries in a 3-second request are not the problem |
| Common-ancestor requirement | Grouping unrelated queries from different phases of a request |

**Generating the fix.** From the fingerprint's AST we know the table and the
filter column. Cross-referencing SQLAlchemy's runtime relationship metadata
produces an actual code suggestion:

```
Detected: 47 × SELECT * FROM posts WHERE author_id = ?
Parent:   GET /api/users/feed
Site:     app/routers/feed.py:88
Cost:     612ms of 780ms total (78%)
Fix:      Post.query.options(selectinload(Post.author))
Effect:   47 queries → 2 queries
```

The same detector, generalised, catches chatty microservice calls — swap
`kind == 'client:db'` for `kind == 'client:http'` and group by normalised URL
template instead of SQL fingerprint. One algorithm, two pathologies.

### 6.3 Detector 2 — Missing index (with real evidence)

```
FOR each expensive fingerprint F (by total time across window):
    ast     ← parse(F)
    tables  ← extract_tables(ast)
    filters ← extract_filter_columns(ast)      # WHERE, JOIN ON, ORDER BY
    plan    ← shadow_db.explain(F, sample_params)
    FOR each node in plan:
        IF node.type == 'Seq Scan' AND node.rows_removed_by_filter > THRESHOLD:
            col ← filter column on node.relation
            IF exists_index_covering(col):     continue    # already indexed
            selectivity ← node.rows_out / node.rows_scanned
            IF selectivity > 0.25:             continue    # index won't help
            EMIT Finding(
                type       = "MISSING_INDEX",
                evidence   = plan_excerpt(node),
                ddl        = f"CREATE INDEX CONCURRENTLY idx_{table}_{col} ON {table}({col});",
                verify     = True
            )
```

Every claim is backed by evidence, not inference: the scan type comes from the
actual planner, absence of an index is confirmed against `pg_indexes`,
selectivity is computed (an index on a low-selectivity column is useless and
suggesting one is worse than saying nothing), and `CONCURRENTLY` is in the DDL
because a naive `CREATE INDEX` takes an `ACCESS EXCLUSIVE` lock.

### 6.4 Detector 3 — Connection pool saturation

Often misdiagnosed as "the database is slow" when the DB is idle. Instrument
the pool to record `pool_wait_ns` separately from execution time.

```
wait_ratio = Σ pool_wait_ns / Σ (pool_wait_ns + exec_ns)
IF wait_ratio > 0.30 AND p95(pool_wait_ns) > 50ms:
    EMIT Finding(
        type = "POOL_SATURATION",
        note = "Requests are queuing for connections; DB itself is healthy.",
        fix  = recommend_pool_size(observed_concurrency, mean_hold_time)
    )
```

The recommendation uses Little's Law:
`required_pool ≈ arrival_rate × mean_hold_time`.

### 6.5 Detector 4 — Serial awaits

```
FOR each parent span P:
    io_children ← [c for c in children(P) if c.kind == 'client']
    IF len(io_children) < 2: continue
    IF pairwise_non_overlapping(io_children)
       AND no_data_dependency(io_children):     # heuristic: no shared param values
        savings = sum(durations) - max(durations)
        IF savings > 50ms:
            EMIT Finding("SERIAL_IO", savings_ms=savings,
                         fix="await asyncio.gather(...)")
```

The `no_data_dependency` check is a heuristic and must be **labelled as one in
the output**. Being upfront about which detectors are rigorous and which are
heuristic is itself a quality signal.

### 6.6 Detector 5 — Regression detection (change points, not thresholds)

Static thresholds fire constantly during traffic spikes and miss slow
degradation entirely. Use CUSUM on the per-endpoint p95 series:

```
S₀ = 0
Sₜ = max(0, Sₜ₋₁ + (xₜ − μ₀ − k))
where μ₀ = baseline mean, k = slack (≈ 0.5σ), alarm when Sₜ > h (≈ 5σ)
```

CUSUM accumulates small persistent deviations, catching a 15% regression a
threshold would never see while ignoring single spikes. Correlate the detected
change point with deploy timestamps to attribute the regression to a release.

### 6.7 Ranking findings

```
score = (total_latency_contribution_ms × request_frequency)   ← impact
        × confidence                                           ← 0..1 per detector
        ÷ estimated_fix_effort                                 ← 1=config, 3=query, 5=refactor
```

Deduplicate across detectors: an N+1 on an unindexed column fires both D1 and
D2. Merge into one finding with the N+1 primary and the index secondary,
because fixing the N+1 makes the index moot.

### 6.8 Verification — the closed loop

1. Provision shadow Postgres from a schema dump plus representative data volume
   (row counts matter enormously for planner behaviour — a 100-row table will
   never seq-scan meaningfully).
2. Replay the captured query with the captured parameters. Measure baseline.
3. Apply the proposed change (`CREATE INDEX` / rewritten query).
4. Re-measure. Same params, same warm state, N repetitions, report median.
5. Emit before/after with the actual `EXPLAIN` plan diff.
6. **If improvement < 20%, DISCARD the recommendation and say so.**

Step 6 is the one that matters. A tool that retracts its own bad suggestions is
trustworthy.

```
FINDING #1 · MISSING_INDEX · verified ✓
  Query      SELECT * FROM posts WHERE author_id = ? ORDER BY created_at DESC
  Endpoint   GET /api/users/feed  (18.2k req/day)
  Plan       Seq Scan on posts  (rows removed by filter: 1,847,221)
  Proposed   CREATE INDEX CONCURRENTLY idx_posts_author_created
               ON posts(author_id, created_at DESC);
  VERIFIED   p95  784ms → 31ms   (−96.0%)
             plan  Seq Scan → Index Scan
             est. daily latency saved: 3.8 CPU-hours
```

---

## 7. The SDK — low-overhead instrumentation

### 7.1 Design rules

1. **Never allocate on the hot path more than necessary.** Pre-allocate span
   structs in a pool.
2. **Never do I/O on the request thread.** Spans go into a lock-free ring
   buffer; a background task drains it.
3. **Bounded memory, always.** Ring buffer is fixed-size. On overflow, drop
   spans and increment a counter — never block the application. A profiler that
   causes an outage is worse than no profiler.
4. **Fail open.** Collector down, network partition, malformed config — the app
   must not notice.
5. **Sample intelligently.** Head-sampling loses the rare slow request, which
   is precisely the one that matters.

### 7.2 Tail-based sampling

```
Keep the trace IF:
    duration > p99(endpoint)     OR      # slow ones
    status == error              OR      # broken ones
    db_span_count > 20           OR      # N+1 candidates
    random() < 0.01                      # baseline for stats
```

This retains essentially all diagnostically valuable traces while storing ~2%
of volume. The tradeoff — buffering spans in the collector until the trace
completes — is mitigated with a bounded per-trace buffer and timeout eviction.

### 7.3 Hook points (Python reference)

| Signal | Hook |
|---|---|
| HTTP server span | ASGI middleware wrapping `__call__` |
| DB query span | SQLAlchemy `before_cursor_execute` / `after_cursor_execute` |
| Pool wait time | `Pool.checkout` / `checkin` events |
| Code location | `traceback.extract_stack()` at query time, filtered to app frames, **cached by fingerprint** |
| Outbound HTTP | `httpx` event hooks |
| Async context | `contextvars` for trace propagation across `await` boundaries |

The stack-capture caching detail matters: `extract_stack()` is expensive, so
capture it only on the first occurrence of each fingerprint per process.

---

## 8. Evaluation — how we prove it works

### 8.1 The benchmark application

`sample-shop` — a deliberately realistic e-commerce API with known, deliberately
planted pathologies. The authoritative, current version of this table lives in
[`PATHOLOGIES.md`](PATHOLOGIES.md), including which are planted today.

| # | Planted pathology | Endpoint | Expected detector |
|---|---|---|---|
| 1 | N+1 on order items | `GET /orders` | D1 |
| 2 | N+1 on review author | `GET /products/{id}` | D1 |
| 3 | Missing index on `orders.user_id` | `GET /users/{id}/orders` | D2 |
| 4 | Missing composite index | `GET /feed` | D2 |
| 5 | Pool size 5 under concurrency 50 | all | D3 |
| 6 | Three serial external API calls | `POST /checkout` | D4 |
| 7 | Unbounded query, no LIMIT | `GET /admin/export` | D2/D6 |
| 8 | Chatty service fan-out | `GET /dashboard` | D1-generalised |
| — | **Control: 12 healthy endpoints** | | **must produce no findings** |

The control group is essential. A detector that flags everything has perfect
recall and zero value.

### 8.2 Metrics

**Detection quality** — recall (target ≥ 7/8), precision (target ≥ 0.90), false
positives on control endpoints (target 0), time-to-detection.

**Recommendation quality** — verification pass rate, measured improvement per
applied fix.

**System performance** — SDK overhead (p50/p95/p99 latency delta, CPU delta,
memory ceiling; target < 1 ms p99, < 2% CPU), ingest throughput, analysis
latency, storage bytes per span after compression.

### 8.3 Load profile

k6 scenarios: steady state (200 RPS, Poisson arrivals); diurnal (sine-wave over
a compressed "day"); burst (10× spike for 60 s, testing sampling and buffer
behaviour); deploy simulation (inject a regression mid-run, verify D5 catches
it).

### 8.4 The headline result

The README should eventually open with a table of the form below. **It is
reproduced here as a template, not as a claim** — real numbers live in
[`RESULTS.md`](RESULTS.md) and are produced by `eval/run_evaluation.py`.

```
Planted pathologies:     8
Detected:                ?   (recall ?)
False positives:         ?   (precision ?, N control endpoints)
Additional issues found: ?   (unplanted)
Verification pass rate:  ?
Median improvement:      ?
SDK overhead:            ?
Ingest:                  ?
```

"Plus N I didn't plant" is the single best sentence available to this project.
It proves the tool generalises beyond what it was tuned for.

---

## 9. Implementation plan — 4 weeks

**Week 1 — Instrumentation & pipeline**

| Day | Deliverable |
|---|---|
| 1 | `sample-shop` app skeleton, schema, seed data at realistic volume (≥1M rows in posts/orders) |
| 2 | SDK: ASGI middleware, span model, contextvars propagation |
| 3 | SDK: SQLAlchemy hooks, pool instrumentation, cached code-location capture |
| 4 | Ring buffer + async OTLP exporter; fail-open behaviour; overflow counters |
| 5 | Go collector: OTLP ingest → ClickHouse write |
| 6 | ClickHouse schema, partitioning, TTL; verify compression ratio |
| 7 | **Milestone:** overhead benchmark. Measure and record. Do not proceed until C1 is met. |

**Week 2 — Trace assembly & first detector**

| Day | Deliverable |
|---|---|
| 8 | Trace assembly (span list → tree), handling out-of-order and orphan spans |
| 9 | SQL fingerprinting with `sqlglot`; unit tests across dialects, CTEs, subqueries |
| 10 | Tail-based sampling in the collector, with bounded per-trace buffering |
| 11–12 | N+1 detector, including all false-positive guards |
| 13 | Generalise to chatty-HTTP; ORM metadata → fix suggestion |
| 14 | **Milestone:** detects planted pathologies #1, #2, #8 with zero control FPs |

**Week 3 — Plan analysis & verification**

| Day | Deliverable |
|---|---|
| 15 | Shadow DB provisioning; schema + volume replication |
| 16 | `EXPLAIN` parsing, plan node model, seq-scan detection |
| 17 | Missing-index detector with selectivity + existing-index checks |
| 18 | Pool saturation + serial-await detectors |
| 19 | CUSUM regression detector; deploy-marker correlation |
| 20 | Verification sandbox — apply, re-measure, discard failures |
| 21 | **Milestone:** closed loop working end to end on ≥1 finding |

**Week 4 — Evaluation, polish, narrative**

| Day | Deliverable |
|---|---|
| 22 | Finding ranker + cross-detector dedup |
| 23–24 | REST API; dashboard (endpoint list → trace waterfall → findings). Hard stop. |
| 25 | k6 scenarios; full evaluation run; results table |
| 26 | README with architecture diagram, headline metrics, `docker compose up` demo |
| 27 | DESIGN.md, write-up on the hardest bug |
| 28 | Rehearse the 3-minute / 10-minute / 25-minute explanations |

**Cut list** (drop in this order): dashboard → CLI that prints findings;
Detector 5 (regression); Detector 4 (serial awaits); Go collector → Python.

**Never cut:** the N+1 detector, the verification loop, or the evaluation
methodology. Those three are the project.

---

## 10. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Scope explosion — building "an APM" instead of a detector | High | The cut list. Dashboard time-boxed. Re-read §2.4 weekly. |
| False positives make it look unreliable | Medium | Control endpoints; guards documented per detector; confidence scores exposed |
| Shadow DB planner diverges from prod | Medium | Replicate row counts and run `ANALYZE`; state the limitation explicitly |
| Overhead exceeds budget | Medium | Measured at the Week 1 milestone, before anything is built on top |
| Tail sampling buffer grows unbounded on trace fragments | Medium | Per-trace cap + timeout eviction + metric on evictions |
| Detectors overfit to planted bugs | Medium | The "found N unplanted" test. If it finds zero unplanted, the detectors are too narrow. |
| Sounds like a Datadog clone | Medium | §2.3 positioning statement, memorised |

---

## 11. Interview narrative

**The 30-second version.** "Observability tools show you evidence and expect you
to diagnose. I built the diagnosis layer — an engine that consumes
OpenTelemetry traces and structurally identifies specific anti-patterns like
N+1 queries and missing indexes, then verifies each proposed fix against a
shadow database before recommending it."

| Question | Where the answer lives |
|---|---|
| How is this different from Datadog? | §2.3 |
| How do you avoid false positives? | §6.2 guard table |
| What's the overhead? | §7, §8.2 |
| How do you know it works? | §8 |
| Why ClickHouse? | §5.2 |
| Why not just regex the SQL? | §6.1 |
| What breaks at 100× scale? | Collector becomes the bottleneck → shard by `trace_id`, since all detectors are trace-local and therefore trivially parallel. Analysis is already batch. Storage: tiered retention. |

**The four sentences that carry the most weight:**

1. "It found two bugs I hadn't planted."
2. "Every recommendation is empirically verified before it's shown; the ones
   that fail verification are discarded."
3. "Instrumentation overhead is X ms at p99 and Y% CPU — I measured it before
   building anything else."
4. "Collection is a solved problem so I used the OTel standard; the analysis
   engine is my contribution."

Sentences 1 and 3 contain numbers. Those numbers get filled in from
`RESULTS.md`, once measured, and not before.

---

## 12. Repository layout

```
aperture/
├── README.md                  # headline metrics first, demo second
├── DESIGN.md                  # this document
├── PATHOLOGIES.md             # documents each planted bug — proves rigor
├── RESULTS.md                 # the table from §8.4
├── docker-compose.yml         # one command to run everything
├── sdk/
│   ├── aperture/
│   │   ├── middleware.py      # ASGI
│   │   ├── hooks/             # sqlalchemy, httpx, pool
│   │   ├── buffer.py          # bounded ring buffer
│   │   └── exporter.py        # OTLP batch export
│   └── benchmarks/            # overhead measurement — keep results in repo
├── collector/                 # Go
├── analyzer/
│   ├── assembly.py            # spans → trace tree
│   ├── fingerprint.py         # sqlglot normalization
│   ├── detectors/
│   │   ├── base.py            # plugin contract
│   │   ├── n_plus_one.py
│   │   ├── missing_index.py
│   │   ├── pool_saturation.py
│   │   ├── serial_io.py
│   │   └── regression.py
│   ├── ranker.py
│   └── verifier/              # shadow DB, apply, re-measure
├── dashboard/
├── sample-shop/               # benchmark app with planted pathologies
├── loadtest/                  # k6 scenarios
└── eval/
    ├── run_evaluation.py
    └── ...
```

`PATHOLOGIES.md` and `RESULTS.md` are small files that do enormous work — they
show that an experiment was designed, not just that code was written.

---

## 13. Stretch goals (only after §9 is complete)

- **CI integration** — run against a PR's test suite, comment when a new N+1 is
  introduced. Turns diagnosis into prevention.
- **Auto-PR generation** — for verified index recommendations, open a migration
  PR automatically.
- **Multi-service traces** — propagate context across service boundaries,
  detect cross-service N+1.
- **eBPF-based zero-instrumentation collection** — capture at the syscall
  layer, no SDK required.
- **LLM-assisted fix synthesis** for the heuristic detectors — only as a
  suggestion layer on top of structurally-confirmed findings, never as the
  detector itself. The value of this project is that it does not guess; do not
  undermine that.

---

## 14. Summary

The project's defensibility rests on four claims, in order of importance:

1. **Structural detection, not statistical smells.** N+1 has a precise
   definition in trace-tree terms and we implement that definition, guards
   included.
2. **Evidence-backed recommendations.** Index advice comes from the actual
   query planner plus a selectivity calculation, never from "the DB looks busy".
3. **A closed verification loop.** Recommendations are tested before they are
   shown, and failures are discarded.
4. **A real evaluation methodology.** Planted pathologies, control group,
   precision and recall, published overhead numbers.

---

## 15. Deviations from v1.0

Recorded deliberately, with reasons, rather than applied silently.

### 15.1 Dashboard: vanilla HTML/CSS/JS instead of React

**v1.0 said:** React + TanStack Query + Recharts.

**We are building:** plain `index.html`, `styles.css`, `app.js`, talking to the
REST API with `fetch()`. Separate files; no bundler, no framework, no component
library.

**Why:** the dashboard is time-boxed and sits at the top of the cut list. A
build toolchain is overhead on a deliverable that is explicitly allowed to be
replaced by a CLI. The dashboard's job is to render real data from the real
API; nothing about that requires a framework, and the constraint keeps it from
quietly expanding into the project's centre of gravity.

**Consequence:** charting is hand-written rather than Recharts. The trace
waterfall and the findings list are the visualisations that matter, and both
are layout problems rather than charting problems.

### 15.2 A `follows` table exists in `sample-shop`

**v1.0's schema sketch** did not name one. `GET /feed` needs a set of authors
to filter on, and deriving it from the order history would make the feed
endpoint read `orders.user_id` and trip pathology P3 as well as P4 — two causes
on one endpoint, which would break attribution in the evaluation. See
`PATHOLOGIES.md` (P4). A follow graph is also what a real community feed uses.

### 15.3 `categories` table

Not in the v1.0 sketch. A product catalogue without categories is not a
believable catalogue, and the category filter is one of the control endpoints'
indexed read paths.

### 15.4 The SDK does not memoise code locations by fingerprint

**v1.0 said** (§7.3): stack capture is expensive, so capture the call site once
per fingerprint and reuse it.

**We capture on every query**, with the memo available behind
`APERTURE_CODE_LOCATION_CACHE=true`.

**Why:** the premise does not hold for this implementation. §7.3's advice is
about `traceback.extract_stack`, which reads source files off disk. The SDK
walks frames directly, reading only `co_filename` and `f_lineno` — measured at
**1.4 µs against a 2.2 ms query, 0.06%**.

What the memo costs is correctness on the flagship case. Two call sites issuing
byte-identical SQL share one entry, so the second inherits the first's
location. In sample-shop the auth dependency and the product-page N+1 both run
`SELECT ... FROM users WHERE id = $1`; with the memo on, the N+1 finding names
the auth dependency. §6.2's promised output is `site: app/routers/feed.py:88`,
and a wrong file there is worse than no file.

### 15.5 Span timestamps come from the monotonic clock

**Not addressed in v1.0.** On Windows `time.time_ns()` has 15.625 ms
resolution — measured here, 20,000 consecutive calls returned two distinct
values.

§6.2's non-overlap guard and all of §6.5 are statements about whether spans
overlap in time. At 15 ms granularity, twenty sibling queries of 2 ms each
carry identical timestamps, and an N+1 becomes indistinguishable from a
deliberate `asyncio.gather` fan-out — the single confusion that would turn the
flagship detector into a coin flip.

Span times are derived from `perf_counter_ns` against one wall-clock anchor.
Absolute times then drift from the system clock at whatever rate the two
oscillators differ; ordering and durations *within* a trace are exact. Trace
analysis only asks the second kind of question.

### 15.6 Fingerprints are 63-bit

**Not addressed in v1.0**, which specifies `xxhash64` and a `UInt64` column.

OTLP carries integer attributes in a protobuf `int64`, which is signed, and
protobuf raises while serialising an out-of-range value — taking the whole
batch with it, not just the offending span. Found by pointing the SDK at a real
OTLP receiver: 90 of 200 sample fingerprints were out of range.

Masking to 63 bits yields a value that is simultaneously a valid positive
`int64` and a valid ClickHouse `UInt64`, so no layer has to reinterpret
two's-complement bit patterns. The cost is one bit of hash space: the birthday
bound moves to roughly 3 billion distinct fingerprints, against the thousands
this system will see. §5.2's `UInt64` column is unchanged and still correct.

### 15.7 The SDK does not depend on `opentelemetry-sdk`

**v1.0 said** (§5.1): use OTLP over gRPC, and do not reinvent tracing.

**We use** `opentelemetry-proto` — the official protobuf schema, so the wire
format is genuinely OTLP and a stock OTel collector can read it — but implement
the span model, buffering and export ourselves.

**Why:** the parts of `opentelemetry-sdk` we would be adopting are exactly the
parts constraints C1 and C3 are about. `BatchSpanProcessor` uses an unbounded
queue with a blocking fallback; C3 requires a fixed ring that drops and
increments a counter. The SDK is also installed into somebody else's
application, where every transitive dependency is a constraint imposed on a
host we do not own.

The spirit of §5.1 is "do not invent a wire format", and we have not.
