# Planted pathologies

Aperture is evaluated against `sample-shop`, a benchmark e-commerce API whose
performance bugs are known in advance. This file is the experiment's ground
truth: it records what is wrong with which endpoint, why, and which detector is
supposed to find it.

Two rules govern this file.

**Nothing here is a detection.** The analysis engine does not exist yet. These
are planted defects, documented by the person who planted them. When detectors
start reporting findings, those findings get compared against this table — that
comparison is what produces the recall and precision numbers in `RESULTS.md`.

**Status is reported honestly.** A pathology is `PLANTED` only if it is in the
code or schema right now and something verifies it. Anything else is `PENDING`
with the milestone it belongs to.

---

## Why the endpoints are not contrived

Every pathological endpoint below is a feature a real shop needs — an order
queue, a product page, a customer's order history, a follow feed, a finance
export. None of them exists to demonstrate a detector. The bugs are the ones
that arrive naturally when those features are written the obvious way:
lazy-loading in a loop, a foreign key without an index, an export with no
pagination.

This matters for the evaluation. A detector tuned against artificial endpoints
proves nothing. A detector that finds bugs in code that looks like ordinary
application code is making a real claim.

---

## Summary

| # | Pathology | Endpoint | Detector | Status |
|---|-----------|----------|----------|--------|
| P1 | N+1 on order line items | `GET /api/orders` | D1 structural | **PLANTED** |
| P2 | N+1 on review authors | `GET /api/products/{id}` | D1 structural | **PLANTED** |
| P3 | Missing index on `orders.user_id` | `GET /api/users/{id}/orders` | D2 plan-based | **PLANTED** |
| P4 | Missing composite index on `posts` | `GET /api/feed` | D2 plan-based | **PLANTED** |
| P5 | Connection pool saturation | all endpoints under load | D3 pool | PENDING — wait time now measured, needs load |
| P6 | Three serial external calls | `POST /api/checkout` | D4 serial I/O | PENDING — needs partner services |
| P7 | Unbounded result set | `GET /api/admin/export` | D2/D6 row-count | **PLANTED** |
| P8 | Chatty service fan-out | `GET /api/dashboard` | D1 generalised to HTTP | PENDING — endpoint not built |

Planted so far: **5 of 8**.

---

## P1 — N+1 on order line items

| | |
|---|---|
| **Endpoint** | `GET /api/orders` (the fulfilment queue) |
| **Code** | `sample-shop/shop/routers/orders.py` → `list_orders` |
| **Detector** | D1, the flagship structural detector |
| **Status** | PLANTED |

**What it does.** The orders themselves are fetched in one indexed query
against `idx_orders_placed_at`. The line items are then fetched one order at a
time, inside the loop, so a page of 20 orders costs 21 round trips instead of 2.

**Why this is the right shape for D1.** DESIGN.md §6.2 defines an N+1 as N
sibling DB spans under a common ancestor, sharing a fingerprint, executing
sequentially, each returning few rows, with varying bound parameters. All five
clauses hold here on purpose:

- *common ancestor* — every item query is a child of the same request span
- *shared fingerprint* — identical SQL, only the bound `order_id` differs
- *sequential* — a plain `for` loop, no `asyncio.gather`
- *varying parameters* — a different `order_id` each iteration
- *few rows each* — orders average 1–3 line items

**Deliberately isolated from other causes.** `order_items.order_id` **is**
indexed (`idx_order_items_order`). If it were not, this endpoint would trigger
both D1 and D2 and the evaluation could not attribute the finding to a cause.
The join to `products` is folded into the same statement for the same reason:
exactly one extra round trip per order, one fingerprint to detect.

**Expected fix.** `selectinload(Order.items)` — 21 queries collapse to 2.

**Control for comparison.** `GET /api/orders/{id}` returns byte-identical data
for a single order using `selectinload`, and a test asserts the two endpoints
agree. P1 is a pure performance bug: same output, far more round trips.

**Verified by** `tests/sample_shop/test_planted_pathologies.py`:
`test_p1_order_queue_issues_one_item_query_per_order`,
`test_p1_query_count_grows_with_page_size`.

---

## P2 — N+1 on review authors

| | |
|---|---|
| **Endpoint** | `GET /api/products/{id}` (product detail page) |
| **Code** | `sample-shop/shop/routers/catalog.py` → `get_product` |
| **Detector** | D1 |
| **Status** | PLANTED |

**What it does.** The product, its category and its seller load correctly in one
query with `joinedload`. The ten most recent reviews load in one indexed query.
Then each review's author is resolved individually with `session.get(User, ...)`.

**Why it is realistic.** This is the shape almost every ORM application arrives
at, because the reviews query and the author lookup are usually written months
apart by different people. The first two loads being *correct* is the point:
real N+1s hide next to code that got it right.

**Note on exact counts.** SQLAlchemy's identity map holds weak references, so
when two reviews share an author the second lookup is served from memory only
if the first `User` has not been garbage-collected. The query count is
therefore bounded by the number of reviews rendered rather than exactly equal
to it, and the test asserts the bound rather than an exact number.

**Expected fix.** `selectinload(Review.author)` — one extra query regardless of
review count.

**Control for comparison.** `GET /api/products/{id}/reviews` renders the same
reviews with `selectinload` and issues zero per-row author lookups.

**Verified by** `test_p2_product_page_loads_review_authors_one_at_a_time` and
`test_review_list_is_the_control_for_p2`.

---

## P3 — Missing index on `orders.user_id`

| | |
|---|---|
| **Endpoint** | `GET /api/users/{id}/orders` (customer order history) |
| **Schema** | `sample-shop/shop/models.py` → `Order.__table_args__` |
| **Detector** | D2, plan-based |
| **Status** | PLANTED |

**What it does.** PostgreSQL does not create an index for a foreign key on the
referencing side. `orders.user_id` has none, so both statements this endpoint
issues — the `COUNT` for the pagination total and the page query itself — have
to filter a large table on an unindexed column.

**Why this one.** It is the most common index bug in production ORM
applications, and it is invisible in code review because there is nothing wrong
with the code. The endpoint is otherwise written correctly: bounded page size,
line-item counts fetched in one grouped query rather than per order. It
exercises D2 and nothing else.

**Measured evidence** (PostgreSQL 16.14, `medium` profile, 250,000 orders):

```
EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM orders WHERE user_id = 7;

 Finalize Aggregate (actual time=26.739..29.605 rows=1 loops=1)
   Buffers: shared hit=4556 read=117
   ->  Gather (actual time=26.521..29.594 rows=3 loops=1)
         Workers Planned: 2   Workers Launched: 2
         ->  Partial Aggregate (actual time=10.303..10.305 rows=1 loops=3)
               ->  Parallel Seq Scan on orders (rows=117 loops=3)
                     Filter: (user_id = 7)
                     Rows Removed by Filter: 83216
 Execution Time: 29.850 ms
```

Selectivity is 351 / 250,001 ≈ 0.14%, far below D2's 0.25 unselectivity
cutoff, so the detector should recommend an index and the selectivity check
should agree.

**Two design notes for D2 (Week 3 Day 17), both discovered here:**

1. **Parallel plans report per-worker counts.** At this scale the planner chose
   `Parallel Seq Scan`, so `Rows Removed by Filter: 83216` is per worker across
   `loops=3` — the true figure is ~249,648. A detector that reads the raw
   number without multiplying by `loops` will under-report the damage by the
   worker count, and its selectivity calculation will be wrong.
2. **A missing index does not always present as a `Seq Scan`.** The page query
   takes a different route entirely — the planner walks
   `idx_orders_placed_at` backwards and filters:

   ```
    Limit (actual time=10.042..10.045 rows=20 loops=1)
      ->  Incremental Sort
            ->  Index Scan Backward using idx_orders_placed_at on orders
                  Filter: (user_id = 7)
                  Rows Removed by Filter: 19040
   ```

   D2 must handle both shapes. A detector that only looks for `Seq Scan` misses
   half of this case.

**Scale sensitivity.** At the `small` profile the same COUNT costs ~1.5 ms with
24,877 rows removed — structurally present but not dramatic. At `medium` it is
~30 ms. Evaluation runs use `medium` or larger.

**Expected fix.** `CREATE INDEX CONCURRENTLY idx_orders_user_id ON orders(user_id);`

**Verified by** `test_p3_orders_user_id_is_still_unindexed`, which fails loudly
if anyone adds the index.

---

## P4 — Missing composite index on `posts`

| | |
|---|---|
| **Endpoint** | `GET /api/feed` (personalised community feed) |
| **Schema** | `sample-shop/shop/models.py` → `Post.__table_args__` |
| **Detector** | D2 |
| **Status** | PLANTED |

**What it does.** The feed reads the caller's follow list (fast — it uses the
composite primary key of `follows`), then:

```sql
SELECT * FROM posts
WHERE author_id = ANY(:followed_ids) AND is_published
ORDER BY created_at DESC LIMIT :n
```

`posts` has **no secondary index at all**. PostgreSQL scans the table, discards
the large majority on the author filter, then top-N sorts the remainder.

**Why a `follows` table exists.** The feed needs a set of authors to filter on.
Deriving that set from the user's order history would make this endpoint read
`orders.user_id` and trip P3 as well as P4 — two causes on one endpoint, and an
evaluation that cannot attribute a finding to a cause. A follow graph is also
what a real community feed uses.

**Measured evidence** (`medium` profile, 500,000 posts, user following 20 authors):

```
 Limit (actual time=70.081..74.626 rows=20 loops=1)
   Buffers: shared hit=66 read=25339
   InitPlan 1 (returns $0)
     ->  Index Only Scan using follows_pkey on follows (rows=20 loops=1)
   ->  Gather Merge (actual time=70.078..74.615 rows=20 loops=1)
         Workers Planned: 2   Workers Launched: 2
         ->  Sort (actual time=61.314..61.326 rows=17 loops=3)
               Sort Key: posts.created_at DESC, posts.id DESC
               Sort Method: top-N heapsort  Memory: 43kB
               ->  Parallel Seq Scan on posts (rows=2012 loops=3)
                     Filter: (is_published AND (author_id = ANY ($0)))
                     Rows Removed by Filter: 164654
 Execution Time: 74.914 ms
```

Selectivity is 6,036 / 500,001 ≈ 1.2% — well inside the range where an index
helps. The scan reads 25,339 pages from disk to return 20 rows, which is the
number that makes the case obvious.

Note the same parallel-plan caveat as P3: 164,654 rows removed is per worker
across three loops.

**Expected fix.**
`CREATE INDEX CONCURRENTLY idx_posts_author_created ON posts(author_id, created_at DESC);`

**Verified by** `test_p4_posts_has_no_secondary_index`.

---

## P5 — Connection pool saturation

| | |
|---|---|
| **Endpoint** | every endpoint, under concurrency |
| **Config** | `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` in `.env` |
| **Detector** | D3 |
| **Status** | PENDING — Week 1 Day 7 (needs the load generator) |

**Plan.** The knob already exists and defaults to a healthy pool (20 + 10
overflow). The pathology is created by running the load test at concurrency ~50
against `DB_POOL_SIZE=5`. Requests then spend most of their time waiting for a
connection while PostgreSQL itself sits idle — the case D3 exists to
distinguish from "the database is slow".

**What changed on Day 2.** The measurement now exists. The SDK records
`pool_wait_ns` per span, separately from execution time, and sums it onto the
request span; `Pool.checkout`/`checkin` give the hold time that the Little's
Law recommendation needs. Verified against real contention: two concurrent
requests against a one-connection pool produced a measured wait of **>100 ms**
on the loser, and the wait is attributed once per checkout rather than once per
query, so summing across spans gives the true total.

**What is still missing.** The load generator. Creating the pathology means
running at concurrency ~50 against `DB_POOL_SIZE=5`, which is k6 work (Week 4
Day 25, and exercised at the Day 7 overhead milestone). `max_connections=300`
is already set on the PostgreSQL container so the server is not the bottleneck
when that runs. Until then the input to D3 is instrumented but the condition
has never been provoked, so the pathology is not claimed as planted.

**Expected fix.** Little's Law: `required_pool ≈ arrival_rate × mean_hold_time`.

---

## P6 — Three serial external calls

| | |
|---|---|
| **Endpoint** | `POST /api/checkout` |
| **Code** | `sample-shop/shop/routers/checkout.py` |
| **Detector** | D4, serial I/O |
| **Status** | PENDING — needs real partner services |

**Plan.** Checkout should call three independent partner services — payment
authorisation, tax calculation, and a shipping quote — sequentially when they
could run concurrently under `asyncio.gather`.

**Why it is not planted yet.** D4 detects *span overlap*: it looks for outbound
I/O spans that do not overlap and have no data dependency. Stubbing the partner
calls with `asyncio.sleep` or canned responses would produce spans with no real
I/O behind them, and a detector validated against fake I/O has proved nothing.
The honest sequence is: build small real partner services first, then make
checkout call them serially.

**Today** `POST /api/checkout` is a genuine control endpoint: it validates
ownership and state and moves an order `pending → paid` in one transaction. It
works, and it is correct.

**Expected fix.** `await asyncio.gather(authorise(), quote_tax(), quote_shipping())`.

---

## P7 — Unbounded result set

| | |
|---|---|
| **Endpoint** | `GET /api/admin/export` (finance reconciliation export) |
| **Code** | `sample-shop/shop/routers/admin.py` → `export_orders` |
| **Detector** | D2/D6, from the row-count distribution |
| **Status** | PLANTED |

**What it does.** `since` and `until` are optional. With neither supplied the
endpoint selects every order that has ever been placed — no `LIMIT`, no cursor,
no streaming — materialises the whole result set in the application process,
and serialises it in one response.

**Why it is interesting.** The SQL is perfectly ordinary. There is no bad plan
and no missing index. The only signal is the row-count distribution, which is
why this case belongs to a different detector path than P3 and P4.

**Measured today**: at `small` (25,001 rows) the response is 4.0 MB and takes
~620 ms. At `medium` (250,001 rows) the same call takes **6.3 s** and holds the
entire result set in the application process while it serialises. The response
size grows linearly with the age of the business, which is precisely why this
bug is invisible for the first year.

**Memory note.** This endpoint really can exhaust memory on a large dataset.
That is the pathology, and it stays. It is confined to the benchmark
application — Aperture's own instrumentation is separately required never to
grow without bound (design constraint C3).

**Expected fix.** Mandatory date window plus keyset pagination, or a streaming
response.

**Verified by** `test_p7_export_query_has_no_limit` and
`test_export_returns_every_order`.

---

## P8 — Chatty service fan-out

| | |
|---|---|
| **Endpoint** | `GET /api/dashboard` (seller dashboard) |
| **Detector** | D1 generalised to `kind == 'client:http'` |
| **Status** | PENDING — endpoint not built |

**Plan.** A seller dashboard that fetches per-product statistics from an
internal service one product at a time, instead of using its batch endpoint.
Structurally identical to P1 with HTTP spans in place of DB spans and a
normalised URL template in place of a SQL fingerprint — the same detector,
proving the abstraction was chosen well (DESIGN.md §6.2).

**Why it is not built yet.** Same reason as P6: it needs a real service to be
chatty towards. Building the endpoint now with nothing behind it would mean
committing an endpoint that does nothing, which is worse than not having it.

---

## Control group

The control endpoints are as important as the pathological ones. A detector
that flags everything has perfect recall and no value. **These must produce
zero findings.**

| # | Endpoint | Why it is healthy |
|---|----------|-------------------|
| C1 | `GET /health/live` | No I/O at all |
| C2 | `GET /health/ready` | One `SELECT 1` |
| C3 | `GET /health/info` | No I/O |
| C4 | `GET /api/categories` | Small bounded dimension table |
| C5 | `GET /api/products` | Bounded page, indexed filters, opt-in COUNT, trigram index for search |
| C6 | `GET /api/products/{id}/reviews` | Indexed, paginated, `selectinload` for authors |
| C7 | `POST /api/products/{id}/reviews` | Single insert plus a rollup update |
| C8 | `GET /api/users/{id}` | Primary-key lookup |
| C9 | `GET /api/users/{id}/reviews` | Indexed on `reviews.author_id`, eager-loaded |
| C10 | `GET /api/orders/{id}` | `selectinload` — the correct form of P1 |
| C11 | `POST /api/orders` | Products fetched in one batched query, one transaction |
| C12 | `GET /api/posts/{id}` | Primary-key lookup plus one author load |
| C13 | `POST /api/posts` | Single insert |
| C14 | `GET /api/admin/stats` | Row counts from `pg_class.reltuples`, GMV bounded to 7 days |
| C15 | `POST /api/checkout` | Control until P6 is planted |

That is 15 control endpoints, against DESIGN.md §8.1's requirement of 12.

`GET /api/admin/stats` deserves a note: it is a control *because* it uses
planner estimates rather than `SELECT count(*)`. An exact count of a
multi-million-row table is a sequential scan on every page load. If it were
written the obvious way it would be a ninth pathology, and it would be an
accidental one.

---

## Indicative latencies — pathological vs control

**These are not benchmarks.** One client, no concurrency, warm cache, one
laptop, 15 samples per endpoint, median reported. They are here as evidence
that the planted pathologies are real and material, not as performance results.
Real numbers come from `eval/run_evaluation.py` and land in `RESULTS.md`.

Measured at the `medium` profile (2,012,141 rows, 535 MB on disk), against the
same server, back to back, each pathological endpoint paired with the control
that returns comparable data:

| Endpoint | Role | Median | Ratio |
|---|---|---:|---:|
| `GET /api/orders?limit=20` | **P1** | 50.6 ms | |
| `GET /api/orders/{id}` | C10 control | 12.1 ms | **4.2×** |
| `GET /api/products/{id}` | **P2** | 44.2 ms | |
| `GET /api/products/{id}/reviews` | C6 control | 16.9 ms | **2.6×** |
| `GET /api/users/{id}/orders` | **P3** | 33.2 ms | |
| `GET /api/users/{id}/reviews` | C9 control | 13.3 ms | **2.5×** |
| `GET /api/feed?limit=20` | **P4** | 52.1 ms | |
| `GET /api/products?limit=20` | C5 control | 23.2 ms | **2.2×** |
| `GET /api/users/{id}` | C8 control | 8.9 ms | — |

Every pathological endpoint is 2.2–4.2× slower than the nearest healthy read of
comparable data. That gap is what the detectors have to explain — and, more to
the point, what the verification loop has to close.

The ratios understate the problem for P1: the gap grows with page size, because
the query count does. This measurement uses `limit=20`; a page of 100 costs 101
round trips.

---

## Keeping this file true

Three mechanisms stop the table above from drifting away from reality:

1. **Tests that pin the pathologies.**
   `tests/sample_shop/test_planted_pathologies.py` fails if a planted index
   reappears, if the export gets a `LIMIT`, or if an N+1 gets eager-loaded.
   Each failure message points back here.
2. **Comments at every site.** Every planted defect carries a block comment
   naming its ID and saying not to fix it.
3. **A note in the migration.** `0001_initial_schema.py` documents the two
   absent indexes, so a future migration author sees the warning before writing
   `op.create_index`.

Removing a pathology on purpose is fine. Removing one by accident, and then
reporting recall against a table that no longer describes the code, is the
failure mode these guards exist to prevent.
