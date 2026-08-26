"""Operational endpoints."""

from __future__ import annotations


async def test_liveness_does_not_touch_the_database(client) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "sample-shop"


async def test_liveness_issues_no_sql(client, queries) -> None:
    """A liveness probe that queries PostgreSQL turns a blip into a restart loop."""
    await client.get("/health/live")
    assert len(queries) == 0, f"liveness ran SQL:\n{queries.summary()}"


async def test_readiness_reports_pool_state(client) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    database = response.json()["database"]
    assert database["status"] == "ok"
    assert "pool_size" in database


async def test_info_never_leaks_the_password(client) -> None:
    import os

    response = await client.get("/health/info")
    assert response.status_code == 200
    config = response.json()["config"]

    secret = os.environ.get("POSTGRES_PASSWORD", "shop_local_dev_password")
    assert secret not in response.text
    assert not any("password" in key.lower() for key in config)


async def test_info_reports_the_database_actually_in_use(client) -> None:
    """/health/info must name the effective target, not the configured default.

    DATABASE_URL overrides the individual POSTGRES_* settings, and the test
    suite uses that override. An info endpoint that reported the overridden
    values would point an operator at the wrong database.
    """
    config = (await client.get("/health/info")).json()["config"]
    assert config["postgres_db"] == "shop_test"


async def test_every_response_carries_a_request_id(client) -> None:
    response = await client.get("/health/live")
    assert response.headers["X-Request-Id"]


async def test_incoming_request_id_is_preserved(client) -> None:
    response = await client.get(
        "/health/live", headers={"X-Request-Id": "abc123correlation"}
    )
    assert response.headers["X-Request-Id"] == "abc123correlation"
