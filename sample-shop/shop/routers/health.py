"""Operational endpoints: liveness, readiness, and build/config info."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from shop import __version__
from shop.config import get_settings
from shop.db import check_database
from shop.schemas import HealthOut, InfoOut, ReadinessOut

logger = logging.getLogger("shop.health")

router = APIRouter(tags=["ops"])


@router.get("/health/live", response_model=HealthOut)
async def liveness() -> HealthOut:
    """Process is up. Deliberately touches nothing external.

    A liveness probe that queries the database will restart a healthy
    application whenever the database hiccups, which turns a brief outage into
    a long one.
    """
    return HealthOut(status="ok", service="sample-shop", version=__version__)


@router.get("/health/ready", response_model=ReadinessOut)
async def readiness() -> ReadinessOut:
    """Ready to serve traffic: the pool can hand out a working connection."""
    try:
        db_status = await check_database()
    except Exception as exc:
        logger.warning("readiness check failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc
    return ReadinessOut(status="ok", database=db_status)


@router.get("/health/info", response_model=InfoOut)
async def info() -> InfoOut:
    """Effective configuration, with secrets stripped."""
    return InfoOut(
        service="sample-shop",
        version=__version__,
        config=get_settings().safe_summary(),
    )
