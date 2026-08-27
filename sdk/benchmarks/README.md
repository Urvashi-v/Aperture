# `sdk/benchmarks/` — overhead measurement

```bash
python sdk/benchmarks/overhead.py --micro                    # primitives only, no database
python sdk/benchmarks/overhead.py --requests 600 --rounds 5  # against sample-shop
```

## What this is not

**It is not the Week 1 Day 7 milestone measurement, and its output must not be
quoted as SDK overhead.**

Constraint C1 (≤ 1 ms added p99, ≤ 2% CPU) is a claim about a service under
realistic concurrent load. Validating it needs the k6 scenarios from DESIGN.md
§8.3 driving a real server over a real socket with the collector running, and
CPU measured from outside the process. This script drives the ASGI application
in-process, single-threaded, from one client. It exists so the cost is visible
while the SDK is being built, and so Day 7 starts from something rather than
nothing.

## Methodology

The first version of this script ran three big blocks — baseline, instrumented,
baseline — and reported the difference. That was not good enough, and it is
worth saying why, because the failure was instructive.

On this machine it reported the SDK costing **+8.9 ms at p50**. A properly
interleaved re-run of the same configuration put the delta **below the noise
floor**. The number was machine drift, and the three-block structure had no way
to tell drift from signal. Two subsequent "fixes" were investigated on the
strength of that phantom regression.

So the script now:

- **Interleaves in rounds.** Each round runs a baseline block and an
  instrumented block, alternating which goes first, so a machine that is
  warming up or throttling does not dump all of its drift into whichever
  configuration always ran second.
- **Uses a genuinely uninstrumented baseline.** Instrumentation is installed and
  removed around each instrumented block. It has to be: the SQLAlchemy and pool
  hooks are installed on the *class*, so leaving them in place would have the
  "baseline" paying for span creation on every query and the comparison would
  measure nothing.
- **Computes its own noise floor** from the drift between the first and second
  halves of the baseline samples, and **says so** when the measured delta falls
  inside it. A benchmark that reports a number it cannot distinguish from noise
  is worse than one that reports nothing.
- **Reports percentiles, not means.** An average hides exactly the tail C1 is
  about.
- **Drives ASGI directly**, with no HTTP client, because the SDK instruments
  httpx and the measurement would otherwise include the cost of measuring the
  measurement.

## Reading the output

```
block                         n    p50 ms    p95 ms    p99 ms   mean ms
baseline                    600    38.566    51.549    64.784    40.046
instrumented                600    41.299    54.727    72.659    43.202

Delta
  p50      +2.733 ms   (  +7.1%)
```

Then a verdict line saying whether that delta is inside or outside the run's
own noise floor. If it is inside, the run proved nothing — increase `--rounds`
or use a quieter machine. It does not mean the overhead is zero.

## The micro-benchmark

`--micro` measures the SDK's primitives with no application in the way. It
needs no database and is the fastest way to see whether a change to the hot
path helped.

It models the real steady state by draining the ring when it fills. An earlier
version retained every span, which left hundreds of thousands of live objects
for the garbage collector to rescan and measured GC pressure the SDK does not
actually create — the numbers were roughly 2× too high.

## Current readings

Measured on the development machine (Windows, Python 3.12, PostgreSQL 16 in
Docker, `medium` seed profile). Preliminary, single-client, in-process.

| Operation | Cost |
|---|---:|
| span start + end, including buffer put | ~3.8 µs |
| buffer put | ~0.9 µs |
| placeholder fingerprint | ~1.4 µs |
| code-location capture (through the greenlet boundary) | ~1.4 µs |

Against sample-shop's `GET /api/orders?limit=10`, which issues about 13 queries
and produces about 15 spans per request: **+2.7 ms at p50 on a 38.6 ms
baseline (+7.1%)**, with a measured noise floor of 0.68 ms.

That is comfortably above the C1 budget, and the per-span costs above do not
add up to it — roughly 100 µs of primitives against a 2.7 ms delta. Where the
rest goes is an open question and the first thing Day 7 should answer.
Candidates worth eliminating in order: the exporter thread's gRPC activity
competing for the GIL, contextvar operations in the middleware, and the pool
`connect` wrapper.

No CPU or RSS ceiling has been measured. Nothing here has been run under
concurrency.
