# `eval/` — the evaluation harness

**Status: not started.** Week 4, Day 25.

`run_evaluation.py` drives the whole experiment end to end: seed a dataset,
run the load profile, run the analysis engine, compare the findings against
the ground truth in [`../PATHOLOGIES.md`](../PATHOLOGIES.md), and write
[`../RESULTS.md`](../RESULTS.md).

Two rules:

1. **Every number in RESULTS.md is written by this script.** Nothing is typed
   in by hand, and nothing is carried over from a previous run.
2. **Findings on control endpoints are false positives**, counted as such, with
   no exceptions carved out after the fact.
