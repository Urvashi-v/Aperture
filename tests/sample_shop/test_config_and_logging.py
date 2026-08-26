"""Configuration and structured logging. No database required."""

from __future__ import annotations

import json
import logging

from shop.config import Settings
from shop.logging_config import (
    ConsoleFormatter,
    JsonFormatter,
    configure_logging,
    request_id_var,
)


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="shop.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_database_url_is_assembled_from_parts_when_absent() -> None:
    settings = Settings(
        _env_file=None,
        postgres_host="db.internal",
        postgres_port=6000,
        postgres_db="shopdb",
        postgres_user="alice",
        postgres_password="secret",
        database_url=None,
    )
    assert settings.sqlalchemy_url == (
        "postgresql+asyncpg://alice:secret@db.internal:6000/shopdb"
    )


def test_explicit_database_url_wins() -> None:
    settings = Settings(
        _env_file=None,
        postgres_host="ignored",
        database_url="postgresql+asyncpg://u:p@elsewhere:5432/other",
    )
    assert "elsewhere" in settings.sqlalchemy_url


def test_asyncpg_dsn_tracks_the_sqlalchemy_url() -> None:
    """The seeder and the app must never point at different databases."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@host:5432/db",
    )
    assert settings.asyncpg_dsn == "postgresql://u:p@host:5432/db"


def test_safe_summary_omits_the_password() -> None:
    settings = Settings(_env_file=None, postgres_password="hunter2")
    summary = settings.safe_summary()
    assert "hunter2" not in json.dumps(summary)
    assert "password" not in summary


def test_pool_settings_are_validated() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, db_pool_size=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, db_pool_timeout_s=0)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_json_formatter_emits_one_object_per_line() -> None:
    line = JsonFormatter().format(_record())
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "shop.test"
    assert payload["ts"].endswith("+00:00")


def test_json_formatter_promotes_extra_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record(order_id=42, status="paid")))
    assert payload["order_id"] == 42
    assert payload["status"] == "paid"


def test_json_formatter_includes_the_request_id_when_set() -> None:
    token = request_id_var.set("corr-123")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
        assert payload["request_id"] == "corr-123"
    finally:
        request_id_var.reset(token)


def test_json_formatter_omits_the_request_id_outside_a_request() -> None:
    token = request_id_var.set("")
    try:
        assert "request_id" not in json.loads(JsonFormatter().format(_record()))
    finally:
        request_id_var.reset(token)


def test_json_formatter_survives_unserialisable_values() -> None:
    """Logging must never be the thing that raises."""
    payload = json.loads(JsonFormatter().format(_record(when=object())))
    assert "object object" in payload["when"]


def test_console_formatter_is_single_line() -> None:
    line = ConsoleFormatter().format(_record(order_id=42))
    assert "\n" not in line
    assert "hello world" in line
    assert "order_id=42" in line


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    before = [h for h in root.handlers if h.get_name() != "shop-stdout"]

    configure_logging("INFO", "json")
    configure_logging("INFO", "json")
    configure_logging("DEBUG", "console")

    shop_handlers = [h for h in root.handlers if h.get_name() == "shop-stdout"]
    assert len(shop_handlers) == 1
    assert isinstance(shop_handlers[0].formatter, ConsoleFormatter)

    # Leave the root logger as the test session found it.
    root.removeHandler(shop_handlers[0])
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
