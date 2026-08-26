"""sample-shop — the benchmark application Aperture is pointed at.

This is a deliberately ordinary e-commerce backend. Some of its endpoints
contain performance pathologies that are documented, on purpose, in
PATHOLOGIES.md at the repository root. Nothing in this package knows anything
about Aperture: instrumentation arrives later as a single middleware, per
design constraint C2 (zero application code changes).
"""

__version__ = "0.1.0"
