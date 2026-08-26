# `loadtest/` — k6 scenarios

**Status: not started.** Week 4, Day 25 (and Day 7 for the overhead benchmark).

k6 rather than Locust: the GIL limits how much load a Python generator can
produce, and a load generator that saturates before the target does measures
the wrong thing.

Scenarios from [`../DESIGN.md`](../DESIGN.md) §8.3:

- **steady** — 200 RPS, Poisson arrivals
- **diurnal** — sine-wave amplitude over a compressed "day"
- **burst** — 10× spike for 60 s, to exercise sampling and buffer behaviour
- **deploy** — inject a regression mid-run so D5 has something to catch

The steady scenario is also what plants P5 (pool saturation), by running at
high concurrency against a deliberately undersized `DB_POOL_SIZE`.
