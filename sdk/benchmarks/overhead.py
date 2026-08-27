"""Measure what the SDK costs the application it instruments.

    python sdk/benchmarks/overhead.py --requests 500
    python sdk/benchmarks/overhead.py --requests 2000 --path /api/orders --limit 20
    python sdk/benchmarks/overhead.py --micro

THIS IS NOT THE WEEK 1 DAY 7 MILESTONE MEASUREMENT, and it must not be quoted
as one. Constraint C1 (<= 1 ms added p99, <= 2% CPU) is a claim about a service
under realistic concurrent load, and validating it needs the k6 scenarios from
DESIGN.md 8.3 driving a real server over a real socket, with the collector
running. This script drives the ASGI application in-process, single-threaded,
from one client. It is here so that the cost is visible while the SDK is being
built, and so the Day 7 harness has something to start from.

What it does do honestly:

* **Interleaves.** Baseline, instrumented, baseline again. If the two baseline
  blocks disagree, the machine was drifting and the delta is not trustworthy —
  the script says so rather than reporting a number it does not believe.
* **Drives ASGI directly.** No HTTP client, because the SDK instruments httpx
  and the measurement would include the cost of measuring the measurement.
* **Reports percentiles, not means.** An average hides exactly the tail C1 is
  about.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import os
import statistics
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "sample-shop"))

# Set before any `shop` module is imported: the application builds its logging
# configuration and its module-level app object at import time, so setting
# these later would leave the access log writing over the results table.
os.environ.setdefault("SHOP_LOG_LEVEL", "ERROR")
os.environ.setdefault("SHOP_LOG_FORMAT", "console")
os.environ["APERTURE_SDK_ENABLED"] = "false"


# ---------------------------------------------------------------------------
# ASGI driver
# ---------------------------------------------------------------------------


async def asgi_call(app, path: str, headers: dict[str, str]) -> int:
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in headers.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("benchmark", 80),
    }
    status = 0

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    await app(scope, receive, send)
    return status


async def run_block(app, path: str, headers: dict[str, str], count: int) -> list[float]:
    samples: list[float] = []
    for _ in range(count):
        started = time.perf_counter_ns()
        status = await asgi_call(app, path, headers)
        elapsed = time.perf_counter_ns() - started
        if status >= 400:
            raise SystemExit(f"benchmark request returned HTTP {status} for {path}")
        samples.append(elapsed / 1e6)  # milliseconds
    return samples


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def percentile(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def summarise(name: str, samples: list[float]) -> dict:
    return {
        "name": name,
        "n": len(samples),
        "p50": percentile(samples, 0.50),
        "p95": percentile(samples, 0.95),
        "p99": percentile(samples, 0.99),
        "mean": statistics.fmean(samples) if samples else 0.0,
    }


def print_table(rows: list[dict]) -> None:
    print(f"{'block':<24}{'n':>7}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'mean ms':>10}")
    print("-" * 71)
    for row in rows:
        print(
            f"{row['name']:<24}{row['n']:>7}{row['p50']:>10.3f}"
            f"{row['p95']:>10.3f}{row['p99']:>10.3f}{row['mean']:>10.3f}"
        )


def rss_mb() -> float | None:
    """Resident set size, if the platform makes it cheap to ask."""
    try:
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if not ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return None
            return counters.WorkingSetSize / 1024 / 1024

        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes, macOS bytes.
        return usage / 1024 if sys.platform.startswith("linux") else usage / 1024 / 1024
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Micro-benchmark
# ---------------------------------------------------------------------------


def micro_benchmark(iterations: int = 200_000) -> None:
    """Cost of the SDK's own primitives, with no application in the way."""
    from aperture.buffer import SpanBuffer
    from aperture.config import ApertureConfig
    from aperture.hooks import capture_code_location, placeholder_fingerprint
    from aperture.spans import SpanKind, Tracer

    # A realistically sized ring that gets drained when it fills, rather than
    # one big enough to retain every span. Retaining them would leave hundreds
    # of thousands of live objects for the garbage collector to rescan, and the
    # measurement would be of GC pressure this SDK does not actually create —
    # in a running process the exporter thread drains the ring continuously.
    config = ApertureConfig(enabled=True, service_name="bench", buffer_capacity=4096)
    buffer = SpanBuffer(config.buffer_capacity)
    tracer = Tracer(config, buffer)

    gc.collect()
    started = time.perf_counter_ns()
    for _ in range(iterations):
        span = tracer.start_span("db.select", SpanKind.CLIENT)
        tracer.end_span(span)
        if len(buffer) >= 4096:
            buffer.drain_all()
    span_cost = (time.perf_counter_ns() - started) / iterations
    buffer.reset()

    statement = "SELECT order_items.id, order_items.order_id FROM order_items WHERE order_items.order_id = $1"
    started = time.perf_counter_ns()
    for _ in range(iterations // 10):
        placeholder_fingerprint(statement)
    fingerprint_cost = (time.perf_counter_ns() - started) / (iterations // 10)

    started = time.perf_counter_ns()
    for _ in range(iterations // 100):
        capture_code_location(config, skip=1)
    location_cost = (time.perf_counter_ns() - started) / (iterations // 100)

    small = SpanBuffer(1024)
    from aperture.spans import Span

    sample = Span(
        trace_id=1, span_id=1, parent_span_id=0, service="b",
        operation="o", kind=SpanKind.INTERNAL, start_unix_ns=0,
    )
    started = time.perf_counter_ns()
    for _ in range(iterations):
        small.put(sample)
        if len(small) > 512:
            small.drain_all()
    buffer_cost = (time.perf_counter_ns() - started) / iterations

    print("\nMicro-benchmark (per operation)")
    print("-" * 71)
    print(f"  span start+end (incl. buffer put) {span_cost:9.2f} ns")
    print(f"  buffer put                        {buffer_cost:9.2f} ns")
    print(f"  placeholder fingerprint           {fingerprint_cost:9.2f} ns")
    print(f"  code-location capture             {location_cost:9.2f} ns")
    per_query = span_cost + fingerprint_cost + location_cost
    print(
        f"\n  A single instrumented query pays roughly {per_query / 1000:.1f} us of the\n"
        "  above. Compare that against a database round trip (milliseconds), not\n"
        "  against the individual rows. These are in-process costs on an idle\n"
        "  machine and are not a validation of constraint C1."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    import aperture
    from aperture.config import ApertureConfig
    from shop.db import dispose_engine, init_engine
    from shop.main import create_app

    headers = {"X-User-Id": str(args.user_id)}
    path = args.path

    init_engine()
    baseline_app = create_app()  # built while the SDK is off: no middleware

    print(f"Warming up ({args.warmup} requests to {path}) ...")
    await run_block(baseline_app, path, headers, args.warmup)

    config = ApertureConfig(
        enabled=True,
        service_name="sample-shop",
        collector_endpoint=args.collector,
        export_interval_ms=args.export_interval_ms,
        buffer_capacity=args.buffer_capacity,
    )

    per_round = max(1, args.requests // args.rounds)
    baseline: list[float] = []
    instrumented: list[float] = []
    totals: dict[str, int] = {}
    rss_peak = 0.0

    async def baseline_half() -> None:
        baseline.extend(await run_block(baseline_app, path, headers, per_round))

    async def instrumented_half() -> None:
        nonlocal rss_peak
        # Instrumentation is installed and removed around each block. It has to
        # be: the SQLAlchemy and pool hooks are installed on the *class*, so
        # leaving them in place would have the "baseline" paying for span
        # creation on every query and the comparison would measure nothing.
        aperture.instrument(config)
        app = create_app()
        await run_block(app, path, headers, min(args.warmup, 20))
        instrumented.extend(await run_block(app, path, headers, per_round))

        for key, value in aperture.get_stats().items():
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
        current = rss_mb()
        if current is not None:
            rss_peak = max(rss_peak, current)
        aperture.shutdown(timeout=1.0)

    for round_index in range(args.rounds):
        # Alternate which half runs first. A machine that is slowly warming up
        # or thermally throttling would otherwise dump all of its drift into
        # whichever config always ran second.
        if round_index % 2 == 0:
            await baseline_half()
            await instrumented_half()
        else:
            await instrumented_half()
            await baseline_half()
        print(f"  round {round_index + 1}/{args.rounds} complete", flush=True)

    await dispose_engine()

    rows = [summarise("baseline", baseline), summarise("instrumented", instrumented)]
    print()
    print_table(rows)

    print("\nDelta")
    print("-" * 71)
    for key in ("p50", "p95", "p99"):
        delta = rows[1][key] - rows[0][key]
        pct = (delta / rows[0][key] * 100) if rows[0][key] else 0.0
        print(f"  {key:<6} {delta:+8.3f} ms   ({pct:+6.1f}%)")

    # Drift diagnostic: the baseline was sampled throughout the run, so its own
    # two halves should agree. If they do not, the machine moved under us by
    # more than the effect being measured.
    half = len(baseline) // 2
    early, late = summarise("", baseline[:half]), summarise("", baseline[half:])
    drift = abs(early["p50"] - late["p50"])
    drift_ratio = drift / rows[0]["p50"] if rows[0]["p50"] else 0.0
    noise_floor = drift

    if rss_peak:
        print(f"\nPeak RSS while instrumented: {rss_peak:.1f} MB")

    print("\nSDK counters (summed over rounds)")
    print("-" * 71)
    for key in (
        "spans_started", "spans_finished", "spans_buffered",
        "spans_dropped_buffer_full", "spans_dropped_over_budget",
        "spans_exported", "export_failures", "spans_dropped_export_failure",
        "pool_checkouts",
    ):
        if key in totals:
            print(f"  {key:<32}{totals[key]:>12}")

    print("\n" + "=" * 71)
    print(
        f"Baseline drift across the run: {drift:.3f} ms at p50 "
        f"({drift_ratio * 100:.1f}%)."
    )
    measured = rows[1]["p50"] - rows[0]["p50"]
    if abs(measured) <= noise_floor:
        print(
            f"The measured delta ({measured:+.3f} ms) is INSIDE that noise floor.\n"
            "This run cannot distinguish the SDK's cost from machine noise. That\n"
            "is a statement about the experiment, not evidence that the overhead\n"
            "is zero. Use more rounds, or a quieter machine."
        )
    else:
        print(
            f"The measured delta ({measured:+.3f} ms) is outside that noise floor,\n"
            "so it is a real in-process measurement of THIS workload on THIS\n"
            "machine."
        )
    print(
        "\nEither way this is NOT a validation of constraint C1. C1 is about a\n"
        "service under concurrent load measured from outside the process; that is\n"
        "the Week 1 Day 7 milestone and it has not been run. Do not quote these\n"
        "numbers as SDK overhead."
    )
    print("=" * 71)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure Aperture SDK overhead against sample-shop (preliminary)."
    )
    parser.add_argument("--requests", type=int, default=600)
    parser.add_argument(
        "--rounds",
        type=int,
        default=5,
        help="Interleaved baseline/instrumented rounds. More rounds cancel "
        "more drift; requests are divided between them.",
    )
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--path", default="/api/orders?limit=10")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--collector", default="127.0.0.1:14317")
    parser.add_argument("--export-interval-ms", type=int, default=1000)
    parser.add_argument("--buffer-capacity", type=int, default=8192)
    parser.add_argument(
        "--micro",
        action="store_true",
        help="Run only the micro-benchmark; no database needed.",
    )
    args = parser.parse_args()

    if args.micro:
        micro_benchmark()
        return 0

    micro_benchmark(50_000)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
