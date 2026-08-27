# Aperture

**Observability tools show you evidence. Aperture diagnoses the structural
cause — and verifies that the proposed fix actually improves performance.**

Datadog tells you `/api/orders` has a p95 of 1.4 s. Aperture tells you it is an
N+1 on `order_items.order_id` at `routers/orders.py:89`, that it costs 612 ms of
the 780 ms request, that the fix is `selectinload(Order.items)`, and that when
that fix was applied to a shadow database the p95 went from 784 ms to 31 ms.

Full architecture and algorithms: [`DESIGN.md`](DESIGN.md).

---

## Status

**Day 2 of 28. Benchmark application and instrumentation SDK.**

The application is instrumented and producing real OpenTelemetry traces. The
collector, the analysis engine and the dashboard are still unwritten.

| Component | Status |
|---|---|
| `sample-shop` — benchmark API, schema, seeder | **Working** |
| PostgreSQL infrastructure (Docker Compose) | **Working** |
| Planted pathologies P1–P4, P7 | **Planted and verified** |
| Planted pathologies P5, P6, P8 | Pending — see [`PATHOLOGIES.md`](PATHOLOGIES.md) |
| `sdk/` — instrumentation | **Working** (Week 1, Days 2–4) |
| `collector/` — Go OTLP ingest | Not started (Week 1, Day 5) |
| `analyzer/` — detectors, ranker, verifier | Not started (Weeks 2–3) |
| `dashboard/` — findings UI | Not started (Week 4) |

**No detection results exist, because no detector exists.**
[`RESULTS.md`](RESULTS.md) records every detection and overhead metric as
`UNMEASURED`. In particular **the SDK's overhead has not been validated against
constraint C1** — that is the Week 1 Day 7 milestone, and the preliminary
in-process figure is currently over budget. No number in this repository is
typed in by hand.

---

## What exists today

`sample-shop` is a small but believable e-commerce backend: a catalogue with
categories and sellers, customer orders and line items, product reviews, and a
community feed with a follow graph. It is a normal application — the point is
that it looks like code anybody would write.

Some of its endpoints are slow, on purpose, in ways that are documented in
advance. That documentation is the experiment's ground truth: when the
detectors start reporting findings, those findings get compared against
[`PATHOLOGIES.md`](PATHOLOGIES.md) to produce recall and precision.

**Five of eight pathologies are planted** and each is verified by the test
suite or by a query plan captured from a real PostgreSQL:

| # | Pathology | Endpoint | Evidence |
|---|---|---|---|
| P1 | N+1 on order line items | `GET /api/orders` | one line-item query per order, asserted by test |
| P2 | N+1 on review authors | `GET /api/products/{id}` | one author lookup per review, asserted by test |
| P3 | Missing index on `orders.user_id` | `GET /api/users/{id}/orders` | `Parallel Seq Scan`, ~249,648 rows removed by filter |
| P4 | Missing composite index on `posts` | `GET /api/feed` | `Parallel Seq Scan`, 25,339 pages read to return 20 rows |
| P7 | Unbounded result set | `GET /api/admin/export` | 250,001 rows in one 6.3 s response |

Measured against the `medium` dataset (2,012,141 rows), each pathological
endpoint runs **2.2× to 4.2× slower** than the healthiest control that returns
comparable data. Those ratios, and the query plans behind them, are in
[`PATHOLOGIES.md`](PATHOLOGIES.md).

Alongside them are **15 control endpoints** that must stay healthy. A detector
that flags everything has perfect recall and no value, so the controls are as
load-bearing as the bugs.

## Instrumentation

The application is instrumented by exactly one call, at the end of the app
factory in [`shop/main.py`](sample-shop/shop/main.py):

```python
from aperture import instrument_app
instrument_app(app, service_name="sample-shop", service_version=__version__)
```

That is the entire integration surface — design constraint C2. The engine is
never handed over, no session is wrapped, and no router is touched. SQL
queries, connection-pool waits and outbound HTTP are captured by hooks the SDK
installs on the SQLAlchemy and httpx *classes*.

It is **off unless `APERTURE_SDK_ENABLED=true`**, and when off no middleware is
added at all. Every test in this repository passes identically either way.

A request to `GET /api/orders?limit=6` produces this, on the wire, in OTLP:

```
SERVER  GET /api/orders            57.9ms  endpoint=/api/orders  pool=52.2ms
CLIENT  db.select                   5.5ms  rows=1   at=shop/dependencies.py:43
CLIENT  db.select                   3.6ms  rows=6   at=shop/routers/orders.py:82
CLIENT  db.select                   5.3ms  rows=1   at=shop/routers/orders.py:95
CLIENT  db.select                   1.6ms  rows=1   at=shop/routers/orders.py:95
CLIENT  db.select                   1.2ms  rows=1   at=shop/routers/orders.py:95
CLIENT  db.select                   1.1ms  rows=1   at=shop/routers/orders.py:95
CLIENT  db.select                   1.1ms  rows=1   at=shop/routers/orders.py:95
CLIENT  db.select                   1.2ms  rows=1   at=shop/routers/orders.py:95
```

Six sibling spans sharing one SQL fingerprint, running sequentially, one row
each, from one call site. That is planted pathology P1, and it is the exact
shape detector D1 will look for. Details and the design decisions behind them
are in [`sdk/README.md`](sdk/README.md).

To watch it yourself without the collector (which arrives Day 5):

```bash
python scripts/dev_otlp_sink.py --tree --sql
```

---

## Requirements

| | |
|---|---|
| Python | 3.11 or newer (developed on 3.12) |
| Docker | with Compose v2, for PostgreSQL |
| Bash | for `scripts/` — Git Bash works on Windows |

**No third-party API keys, cloud accounts or paid services are needed.**
Everything runs locally. `.env.example` lists the full configuration surface;
if a later milestone ever needs a real credential it will be added there as a
named, empty variable.

---

## Setup

Five commands from a clean checkout. These are the exact commands, in order.

```bash
cp .env.example .env
```

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e "./sample-shop[dev]" -e "./sdk[dev]"
```

> On macOS or Linux the interpreter is `./.venv/bin/python` instead.
> `./scripts/bootstrap.sh` does this step and picks the right path for you.

```bash
docker compose up -d postgres
```

```bash
cd sample-shop && ../.venv/Scripts/python.exe -m alembic upgrade head
```

```bash
cd sample-shop && ../.venv/Scripts/python.exe -m shop.seed.cli --profile small
```

Then run it:

```bash
cd sample-shop && ../.venv/Scripts/python.exe -m uvicorn shop.main:app --port 8000
```

The API is on <http://127.0.0.1:8000>, with interactive docs at
<http://127.0.0.1:8000/docs>.

### The same thing via scripts

```bash
./scripts/bootstrap.sh && ./scripts/dev_up.sh && ./scripts/migrate.sh && ./scripts/seed.sh && ./scripts/run_api.sh
```

---

## Verify it works

Run the test suite (needs PostgreSQL up; it creates and drops its own
`shop_test` database and never touches your development data):

```bash
./.venv/Scripts/python.exe -m pytest
```

With the server running, hit every endpoint and check the status codes:

```bash
./scripts/smoke.sh
```

Both currently pass in full: **276 tests** and **27/27 smoke checks**.

The suite is not shy about what it talks to: real PostgreSQL, a real OTLP/gRPC
server, real sockets for the outbound-HTTP hook, and real connection-pool
contention for the pool-wait measurement. Two of the planted pathologies are
properties of the PostgreSQL planner, and the SDK's job is to describe what
actually happened, so substituting a fake for any of those would be testing
something other than the system.

To watch the instrumented application end to end, run the OTLP sink and the
server together:

```bash
python scripts/dev_otlp_sink.py --tree
```

```bash
APERTURE_SDK_ENABLED=true ./scripts/run_api.sh
```

---

## Seeding

Dataset size is an experimental variable, not a convenience setting. The
PostgreSQL planner picks a sequential scan over an index based on estimated row
counts, so a missing index on a 500-row table is invisible and a missing index
on a 5,000,000-row table is catastrophic. The index pathologies only reproduce
at realistic scale.

```bash
cd sample-shop && ../.venv/Scripts/python.exe -m shop.seed.cli --list-profiles
```

| Profile | Users | Products | Orders | Reviews | Posts | ~Total rows |
|---|---|---|---|---|---|---|
| `tiny` | 60 | 150 | 200 | 300 | 400 | 1,910 |
| `small` | 2,000 | 6,000 | 25,000 | 30,000 | 50,000 | 212,000 |
| `medium` | 20,000 | 60,000 | 250,000 | 300,000 | 500,000 | 2,280,000 |
| `large` | 50,000 | 200,000 | 1,200,000 | 1,500,000 | 1,500,000 | 9,900,000 |

`tiny` is for the test suite. `small` is the development default. Evaluation
runs use `medium` or larger.

**On row counts.** Those are *targets*, not claims. Two profiles have actually
been built and measured on this machine: `small` (185,171 rows in 9.5 s) and
`medium` (2,012,141 rows in 109 s, 535 MB on disk). **`large` has never been
run.** To see what is genuinely in your own database:

```bash
cd sample-shop && ../.venv/Scripts/python.exe -m shop.seed.cli --report
```

Seeding is reproducible — the same `--seed` produces a byte-identical dataset —
and destructive: it truncates every table before loading. It uses PostgreSQL
`COPY` through raw asyncpg rather than the ORM, because loading a million rows
through SQLAlchemy would take long enough that nobody would run the large
profile.

---

## The API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health/live` | Liveness. Touches nothing. |
| `GET` | `/health/ready` | Readiness. One `SELECT 1`, plus pool statistics. |
| `GET` | `/health/info` | Effective config, password stripped. |
| `GET` | `/api/categories` | |
| `GET` | `/api/products` | Browse: category, text search, price range, opt-in total |
| `GET` | `/api/products/{id}` | Product detail — **P2** |
| `GET` | `/api/products/{id}/reviews` | Paginated reviews (the control for P2) |
| `POST` | `/api/products/{id}/reviews` | Write a review, update the rating rollup |
| `GET` | `/api/users/{id}` | |
| `GET` | `/api/users/{id}/orders` | Order history — **P3** |
| `GET` | `/api/users/{id}/reviews` | |
| `GET` | `/api/orders` | Fulfilment queue — **P1** |
| `GET` | `/api/orders/{id}` | Order detail (the control for P1) |
| `POST` | `/api/orders` | Place an order |
| `POST` | `/api/checkout` | Pay for an order — will become **P6** |
| `GET` | `/api/feed` | Personalised feed — **P4** |
| `GET` | `/api/posts/{id}` | |
| `POST` | `/api/posts` | |
| `GET` | `/api/admin/stats` | Row estimates and 7-day GMV |
| `GET` | `/api/admin/export` | Finance export — **P7** |

**Authentication** is an `X-User-Id` header. This is a benchmark application
whose job is to generate realistic *database* traffic, and a full auth stack
would add surface area without changing a single query pattern. It is not a
security mechanism and is not presented as one. The one thing it does
faithfully is what a real session middleware does: one primary-key lookup of
the caller per request.

---

## Repository layout

```
aperture/
├── README.md                  # you are here
├── DESIGN.md                  # architecture and algorithms — the source of truth
├── PATHOLOGIES.md             # every planted bug, and its status
├── RESULTS.md                 # measured results (currently: none, and it says so)
├── docker-compose.yml         # PostgreSQL today; collector + ClickHouse later
├── pytest.ini
├── .env.example
├── sample-shop/               # the benchmark application  ← all of Day 1
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/versions/   # 0001_initial_schema.py
│   └── shop/
│       ├── main.py            # app factory, request-id middleware, error handling
│       ├── config.py          # pydantic-settings
│       ├── db.py              # engine, pool, session lifecycle
│       ├── logging_config.py  # JSON logging on the stdlib
│       ├── models.py          # 8 tables; two indexes deliberately absent
│       ├── schemas.py
│       ├── dependencies.py
│       ├── routers/           # health, catalog, users, orders, feed, checkout, admin
│       └── seed/              # profiles, generators, COPY-based loader, CLI
├── tests/
│   ├── sample_shop/           # 105 tests against a real PostgreSQL
│   └── sdk/                   # 171 tests: real PG, real gRPC, real sockets
├── scripts/                   # bootstrap, dev_up, migrate, seed, run_api, smoke,
│                              # dev_otlp_sink
├── sdk/                       # the instrumentation SDK  <- Day 2
│   ├── aperture/              # middleware, context, spans, buffer, exporter, hooks
│   └── benchmarks/            # overhead measurement (see its README)
├── collector/                 # not started
├── analyzer/                  # not started
├── dashboard/                 # not started
├── loadtest/                  # not started
└── eval/                      # not started
```

---

## Dependency rationale

Every dependency is load-bearing. Nothing was added for cosmetics.

| Package | Why it is here | Why not the alternative |
|---|---|---|
| `fastapi` | HTTP layer. ASGI is what the SDK middleware will wrap. | Required by the design |
| `uvicorn[standard]` | ASGI server to actually run it | — |
| `sqlalchemy[asyncio]` | ORM whose `before_cursor_execute` / pool events the SDK instruments | Required by the design |
| `asyncpg` | Async PostgreSQL driver; also provides `COPY` for the seeder | psycopg2 would mean a second driver just for migrations |
| `alembic` | Schema migrations. The schema is an experimental control, so it must be versioned. | — |
| `pydantic` / `pydantic-settings` | Request/response models; env configuration. Pydantic is already a FastAPI dependency. | — |
| `pytest`, `pytest-asyncio`, `httpx` | Test suite and its ASGI transport | — |
| `opentelemetry-proto` | The official OTLP protobuf schema, so spans are wire-compatible with any OTel collector | Hand-rolling the schema is the "reinvented tracing" mistake DESIGN.md §5.1 warns about |
| `grpcio` | OTLP/gRPC transport | DESIGN.md §5.1 specifies gRPC |

The SDK deliberately does **not** depend on `opentelemetry-sdk`. It is
installed into somebody else's application, so every dependency it drags in is
a constraint imposed on a host it does not own — and its batching, queueing and
sampling are precisely the parts constraints C1 and C3 require us to control
ourselves. A bounded ring that drops and counts is not what a stock
`BatchSpanProcessor` gives you.

Deliberately **not** added:

- **structlog / loguru** — the requirement is one JSON object per line with a
  request-scoped correlation id. That is ~40 lines of `logging.Formatter`
  (`shop/logging_config.py`). A logging framework would not earn its place.
- **Faker** — roughly two orders of magnitude too slow for millions of rows,
  and the realism it buys is not realism this benchmark needs. What the
  benchmark needs is reproducibility from a seed and *skewed* distributions,
  which `shop/seed/generators.py` provides in a few hundred lines.
- **jq** — `scripts/smoke.sh` uses the venv's Python for JSON, so there is no
  extra system dependency.

Later milestones will add `sqlglot` (AST-based SQL fingerprinting — see
DESIGN.md §6.1 on why regex is not acceptable here), a ClickHouse client, and
`scipy`/`ruptures` for the CUSUM detector.

Also deliberately not added: **structlog/loguru** in the SDK (same reasoning as
the application), and **jq** anywhere (`scripts/` use the venv's Python for
JSON).

---

## Production-safety rules

These constrain Aperture itself, and are recorded here because they are easy to
violate quietly:

1. Instrumentation must never block the application because the collector is
   down. Fail open.
2. The SDK's memory must be bounded regardless of traffic. Fixed-size ring
   buffer; on overflow, drop spans and increment a counter.
3. Aperture never writes to production data.
4. Aperture never executes destructive SQL against production. Verification
   runs against a shadow database, and only there.
5. A recommendation that has not been verified is labelled as unverified. One
   that fails verification by less than 20% improvement is discarded, not
   downgraded.

The benchmark application is exempt from the *performance* rules on purpose —
being slow is its job — but not from rule 3. It only ever writes to its own
local database.

---

## Known limitations (Day 1)

- Three of eight pathologies are not planted yet (P5, P6, P8). Two of them need
  real partner services to call; stubbing those would make the serial-I/O
  detector meaningless.
- `small` and `medium` have been built and measured. **`large` has never been
  run** — its row counts in the table above are targets, not claims.
- P3 and P4 are structurally present at `small` scale but cost only ~1.5 ms and
  ~13 ms there. Use `medium` or larger for anything that depends on them.
- `X-User-Id` authentication is not security.
- The seeder is destructive by design and has no incremental mode.
- **SDK overhead is not validated against constraint C1.** The preliminary
  in-process figure is +2.7 ms at p50 on a 38.6 ms request, which is over
  budget, and the per-primitive costs do not account for it. Day 7 is the
  milestone that measures this properly; see
  [`sdk/benchmarks/README.md`](sdk/benchmarks/README.md).
- The SQL fingerprint and the HTTP URL template are both **placeholders**, and
  every span says so in a `*_method` field. The real fingerprint is Week 2
  Day 9.
- The SDK has no span object pool yet (DESIGN.md §7.1.1); spans use `slots=True`
  and nothing more.
- No load testing and no concurrency testing has been done. Every timing in
  this repository is from one laptop, presented as evidence of a query plan or
  a trace shape rather than as a benchmark.
