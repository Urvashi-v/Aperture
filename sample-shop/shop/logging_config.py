"""Structured logging for sample-shop.

Deliberately built on the standard library rather than structlog/loguru: the
requirement is one JSON object per line with a request-scoped correlation id,
which is about forty lines of `logging.Formatter`. Adding a logging framework
for that would not earn its place in the dependency list.

The request id lives in a `contextvars.ContextVar`, which is also the mechanism
the Aperture SDK will use to propagate trace context across `await` boundaries
(design §7.3). Using it here first means the pattern is already proven by the
time instrumentation is added.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Request-scoped correlation id. Empty string when outside a request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Attributes present on every LogRecord; anything else was passed by the caller
# via `extra=` and should be emitted as a structured field.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        payload.update(_extra_fields(record))

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # default=str so a stray datetime/Decimal never takes down logging.
        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line output, for local debugging."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%H:%M:%S.%f"
        )[:-3]
        request_id = request_id_var.get()
        prefix = f"{ts} {record.levelname:<7} {record.name}"
        if request_id:
            prefix += f" [{request_id}]"

        extras = _extra_fields(record)
        suffix = ""
        if extras:
            suffix = "  " + " ".join(f"{k}={v}" for k, v in sorted(extras.items()))

        line = f"{prefix}  {record.getMessage()}{suffix}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single stdout handler on the root logger.

    Idempotent: calling it twice (app startup plus a CLI entry point) will not
    duplicate handlers, which would otherwise double every log line.
    """
    formatter: logging.Formatter = (
        JsonFormatter() if fmt == "json" else ConsoleFormatter()
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)
    handler.set_name("shop-stdout")

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == "shop-stdout":
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own colourised handlers; route its records through
    # ours instead so that every line in the process has the same shape.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # uvicorn.access duplicates the access log our middleware already emits
    # with richer fields, so silence it rather than log every request twice.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
