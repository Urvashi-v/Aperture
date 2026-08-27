"""SDK configuration.

Read from the environment with an `APERTURE_` prefix. Deliberately a plain
dataclass and `os.environ` rather than pydantic-settings: this package is
installed into somebody else's application, and forcing a settings library on
a host that may already have one is exactly the kind of imposition an
instrumentation library should not make.

Every parse below is defensive. A malformed value produces the default and a
warning, never an exception — a typo in an environment variable must not stop
the host application from booting (design rule 7.1.4, fail open).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace

logger = logging.getLogger("aperture.config")

ENV_PREFIX = "APERTURE_"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    logger.warning("ignoring malformed %s%s=%r", ENV_PREFIX, name, raw)
    return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ignoring malformed %s%s=%r", ENV_PREFIX, name, raw)
        return default
    if value < minimum:
        logger.warning(
            "%s%s=%s is below the minimum %s; using the minimum",
            ENV_PREFIX, name, value, minimum,
        )
        return minimum
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring malformed %s%s=%r", ENV_PREFIX, name, raw)
        return default
    return max(value, minimum)


@dataclass(frozen=True)
class ApertureConfig:
    """Effective SDK configuration."""

    # ---- Identity ----------------------------------------------------------
    service_name: str = "unknown-service"
    service_version: str = "0.0.0"
    environment: str = "local"

    # ---- Master switch -----------------------------------------------------
    # Off by default. Instrumentation that turns itself on when a package
    # happens to be installed is how a dependency bump becomes an incident.
    enabled: bool = False

    # ---- Export ------------------------------------------------------------
    collector_endpoint: str = "localhost:4317"
    collector_insecure: bool = True
    export_batch_size: int = 512
    export_interval_ms: int = 1000
    export_timeout_s: float = 5.0
    shutdown_timeout_s: float = 3.0
    # After this many consecutive failures the exporter stops trying for
    # `export_backoff_max_s`, so a collector that is down does not cost the
    # host a connection attempt every interval forever.
    export_failure_backoff_after: int = 3
    export_backoff_max_s: float = 30.0

    # ---- Bounded buffering (constraint C3) ---------------------------------
    # 8192 spans at roughly 400 bytes of Python object each is a few megabytes.
    # The point is that it is a *fixed* few megabytes.
    buffer_capacity: int = 8192

    # ---- Capture detail ----------------------------------------------------
    capture_db_statement: bool = True
    max_statement_chars: int = 4096
    capture_code_location: bool = True
    # Off by default, and that is a deviation from DESIGN.md 7.3 made on the
    # strength of a measurement. The design says to cache the call site per
    # fingerprint because stack capture is expensive - true of
    # `traceback.extract_stack`, which reads source files, but not of the frame
    # walk this SDK uses, which touches only `co_filename` and `f_lineno`.
    # Measured on this project: 2.9 us per capture against a 2.2 ms query,
    # i.e. 0.13%.
    #
    # What the cache costs is correctness on the case that matters most. Two
    # call sites issuing byte-identical SQL share a cache entry, so the second
    # inherits the first's location. In sample-shop the auth dependency and the
    # N+1 loop on the product page both run
    # `SELECT ... FROM users WHERE id = $1`, and with caching on, the N+1
    # finding points at the auth dependency - the tool would be wrong in
    # exactly the place a user would check first.
    #
    # Turn it on for a host where 3 us per query is genuinely material, and
    # read locations as "where this SQL was first seen" when you do.
    code_location_cache: bool = False
    # Bounded, because an unbounded memo of every statement ever seen is a
    # memory leak wearing a cache costume.
    code_location_cache_max: int = 2048
    # Stack frames to walk looking for application code before giving up.
    code_location_max_depth: int = 40
    max_attributes_per_span: int = 48
    max_attribute_chars: int = 1024
    # Spans per trace. A runaway loop must not produce an unbounded trace.
    max_spans_per_trace: int = 2048

    # ---- Sampling ----------------------------------------------------------
    # 1.0 on purpose. DESIGN.md 7.1.5: head sampling discards the rare slow
    # request, which is the one that matters. Sampling is the collector's job,
    # where the whole trace is available to decide on (tail sampling, 7.2).
    # This knob exists as an escape hatch for a host that cannot afford full
    # capture, not as a recommended setting.
    head_sample_rate: float = 1.0

    # ---- Diagnostics -------------------------------------------------------
    # When set, the middleware answers this exact path with the SDK's own
    # counters as JSON. Off by default: an application should not sprout an
    # endpoint it did not ask for.
    stats_path: str | None = None

    # Frames under these prefixes count as "application code" for code-location
    # capture. Empty means "anything that is not this SDK or a known library".
    app_path_prefixes: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, **overrides: object) -> ApertureConfig:
        """Build configuration from the environment, then apply overrides."""
        prefixes = _env("APP_PATH_PREFIXES", "") or ""
        config = cls(
            service_name=_env("SERVICE_NAME", "unknown-service") or "unknown-service",
            service_version=_env("SERVICE_VERSION", "0.0.0") or "0.0.0",
            environment=_env("ENVIRONMENT", "local") or "local",
            enabled=_env_bool("SDK_ENABLED", False),
            collector_endpoint=_env("COLLECTOR_ENDPOINT", "localhost:4317")
            or "localhost:4317",
            collector_insecure=_env_bool("COLLECTOR_INSECURE", True),
            export_batch_size=_env_int("EXPORT_BATCH_SIZE", 512, minimum=1),
            export_interval_ms=_env_int("EXPORT_INTERVAL_MS", 1000, minimum=10),
            export_timeout_s=_env_float("EXPORT_TIMEOUT_S", 5.0, minimum=0.1),
            shutdown_timeout_s=_env_float("SHUTDOWN_TIMEOUT_S", 3.0, minimum=0.0),
            export_failure_backoff_after=_env_int(
                "EXPORT_FAILURE_BACKOFF_AFTER", 3, minimum=1
            ),
            export_backoff_max_s=_env_float("EXPORT_BACKOFF_MAX_S", 30.0, minimum=0.1),
            buffer_capacity=_env_int("BUFFER_CAPACITY", 8192, minimum=1),
            capture_db_statement=_env_bool("CAPTURE_DB_STATEMENT", True),
            max_statement_chars=_env_int("MAX_STATEMENT_CHARS", 4096, minimum=64),
            capture_code_location=_env_bool("CAPTURE_CODE_LOCATION", True),
            code_location_cache=_env_bool("CODE_LOCATION_CACHE", False),
            code_location_cache_max=_env_int(
                "CODE_LOCATION_CACHE_MAX", 2048, minimum=16
            ),
            code_location_max_depth=_env_int(
                "CODE_LOCATION_MAX_DEPTH", 40, minimum=4
            ),
            max_attributes_per_span=_env_int("MAX_ATTRIBUTES_PER_SPAN", 48, minimum=1),
            max_attribute_chars=_env_int("MAX_ATTRIBUTE_CHARS", 1024, minimum=16),
            max_spans_per_trace=_env_int("MAX_SPANS_PER_TRACE", 2048, minimum=1),
            head_sample_rate=min(_env_float("HEAD_SAMPLE_RATE", 1.0), 1.0),
            stats_path=_env("STATS_PATH") or None,
            app_path_prefixes=tuple(
                os.path.normcase(os.path.abspath(p.strip()))
                for p in prefixes.split(os.pathsep)
                if p.strip()
            ),
        )
        if overrides:
            config = replace(config, **overrides)  # type: ignore[arg-type]
        return config

    def describe(self) -> dict[str, object]:
        """Human-readable snapshot, for logs and the diagnostics endpoint."""
        return {
            "service_name": self.service_name,
            "service_version": self.service_version,
            "environment": self.environment,
            "enabled": self.enabled,
            "collector_endpoint": self.collector_endpoint,
            "buffer_capacity": self.buffer_capacity,
            "export_batch_size": self.export_batch_size,
            "export_interval_ms": self.export_interval_ms,
            "head_sample_rate": self.head_sample_rate,
            "capture_db_statement": self.capture_db_statement,
            "capture_code_location": self.capture_code_location,
            "code_location_cache": self.code_location_cache,
        }
