# `analyzer/` — the analysis engine

**Status: not started.** Weeks 2–3.

This is the project. Everything else exists to feed it.

- `assembly.py` — span list → trace tree, handling out-of-order and orphan spans
- `fingerprint.py` — `sqlglot` AST normalisation and hashing (§6.1). Not regex.
- `detectors/base.py` — the plugin contract, defined before the first detector
  rather than retrofitted
- `detectors/n_plus_one.py` — the flagship, with all five false-positive guards
- `detectors/missing_index.py` — real `EXPLAIN` evidence plus a selectivity check
- `detectors/pool_saturation.py` — measured pool wait time, Little's Law
- `detectors/serial_io.py` — span overlap; output must be labelled heuristic
- `detectors/regression.py` — CUSUM change points, correlated with deploys
- `ranker.py` — impact × confidence ÷ fix effort, with cross-detector dedup
- `verifier/` — shadow DB: apply the fix, re-measure, discard anything under 20%

See [`../DESIGN.md`](../DESIGN.md) §6.
