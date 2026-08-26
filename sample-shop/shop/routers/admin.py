"""Back-office endpoints: dataset statistics and the finance order export."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select, text

from shop.dependencies import CurrentUser, DbSession
from shop.models import Order
from shop.schemas import AdminStatsOut

logger = logging.getLogger("shop.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

_STAT_TABLES = (
    "users",
    "products",
    "orders",
    "order_items",
    "reviews",
    "posts",
)


@router.get("/stats", response_model=AdminStatsOut)
async def dataset_stats(db: DbSession, _user: CurrentUser) -> AdminStatsOut:
    """Row counts and gross merchandise value for the back-office overview.

    Control endpoint, and the reason it stays a control is the row counts come
    from `pg_class.reltuples` rather than `SELECT count(*)`. An exact count of
    a multi-million-row table is a sequential scan every time the page loads;
    the planner's estimate is accurate to within a percent or so after ANALYZE
    and costs a single index lookup on a catalogue table. This is the standard
    production answer for "how many rows are there, roughly".

    The seller count and GMV are exact because both are cheap: sellers are a
    partial index, and GMV is bounded to a 7-day window that idx_orders_placed_at
    covers.
    """
    estimate_stmt = text(
        """
        SELECT relname, GREATEST(reltuples, 0)::bigint AS estimate
        FROM pg_class
        WHERE relname = ANY(:tables) AND relkind = 'r'
        """
    )
    rows = (await db.execute(estimate_stmt, {"tables": list(_STAT_TABLES)})).all()
    estimates = {name: int(count) for name, count in rows}

    sellers = (
        await db.execute(text("SELECT count(*) FROM users WHERE is_seller"))
    ).scalar_one()

    gmv = (
        await db.execute(
            select(func.coalesce(func.sum(Order.total_cents), 0)).where(
                Order.placed_at >= func.now() - text("interval '7 days'"),
                Order.status.in_(("paid", "shipped", "delivered")),
            )
        )
    ).scalar_one()

    return AdminStatsOut(
        users=estimates.get("users", 0),
        sellers=int(sellers),
        products=estimates.get("products", 0),
        orders=estimates.get("orders", 0),
        order_items=estimates.get("order_items", 0),
        reviews=estimates.get("reviews", 0),
        posts=estimates.get("posts", 0),
        gross_merchandise_value_cents=int(gmv),
    )


@router.get("/export")
async def export_orders(
    db: DbSession,
    _user: CurrentUser,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> list[dict[str, Any]]:
    """Order export for the finance reconciliation job.

    ---------------------------------------------------------------------
    PLANTED PATHOLOGY P7 - unbounded result set.

    `since` and `until` are optional, and when they are omitted this exports
    every order that has ever been placed: no LIMIT, no streaming, no cursor.
    The whole result set is materialised in the application process and then
    serialised in one response.

    This is how export endpoints are usually written - it works fine for the
    first year, and then it does not. It is the case detector D2/D6 should
    flag from the row-count distribution rather than from the query plan,
    because the SQL itself is perfectly ordinary.

    Do not add a LIMIT. It is planted. See PATHOLOGIES.md (P7).
    ---------------------------------------------------------------------
    """
    stmt = select(
        Order.id,
        Order.user_id,
        Order.status,
        Order.total_cents,
        Order.currency,
        Order.shipping_country,
        Order.placed_at,
    )
    if since is not None:
        stmt = stmt.where(Order.placed_at >= since)
    if until is not None:
        stmt = stmt.where(Order.placed_at < until)
    stmt = stmt.order_by(Order.placed_at)

    rows = (await db.execute(stmt)).all()

    logger.info(
        "order export produced",
        extra={
            "row_count": len(rows),
            "bounded": since is not None or until is not None,
        },
    )

    return [
        {
            "order_id": row.id,
            "user_id": row.user_id,
            "status": row.status,
            "total_cents": row.total_cents,
            "currency": row.currency,
            "shipping_country": row.shipping_country,
            "placed_at": row.placed_at.isoformat(),
        }
        for row in rows
    ]
