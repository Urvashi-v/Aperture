"""User profile, order history and authored reviews."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from shop.dependencies import DbSession, PageParams
from shop.models import Order, OrderItem, Review, User
from shop.schemas import Page, OrderSummary, ReviewOut, UserOut, UserSummary

logger = logging.getLogger("shop.users")

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int, db: DbSession) -> User:
    """Public profile. Primary-key lookup; control endpoint."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")
    return user


@router.get("/{user_id}/orders", response_model=Page[OrderSummary])
async def list_user_orders(
    user_id: int,
    db: DbSession,
    page: PageParams,
) -> Page[OrderSummary]:
    """A user's order history, newest first.

    ---------------------------------------------------------------------
    PLANTED PATHOLOGY P3 - missing index on orders.user_id.

    Both statements below filter on `orders.user_id`, which has no index (see
    the comment in models.Order.__table_args__). The COUNT in particular has
    no LIMIT to hide behind, so the planner has to read the whole table and
    discard almost all of it.

    The endpoint itself is written correctly - bounded page, item counts
    fetched in one grouped query rather than per order. That isolation is
    deliberate: this endpoint must exercise the missing-index detector and
    nothing else, so that the evaluation can attribute a finding to a cause.

    See PATHOLOGIES.md (P3).
    ---------------------------------------------------------------------
    """
    total = (
        await db.execute(
            select(func.count()).select_from(Order).where(Order.user_id == user_id)
        )
    ).scalar_one()

    order_stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.placed_at.desc(), Order.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    orders = list((await db.execute(order_stmt)).scalars().all())

    counts: dict[int, int] = {}
    if orders:
        count_stmt = (
            select(OrderItem.order_id, func.count())
            .where(OrderItem.order_id.in_([o.id for o in orders]))
            .group_by(OrderItem.order_id)
        )
        counts = {row[0]: row[1] for row in (await db.execute(count_stmt)).all()}

    items = [
        OrderSummary(
            id=o.id,
            status=o.status,
            total_cents=o.total_cents,
            currency=o.currency,
            placed_at=o.placed_at,
            item_count=counts.get(o.id, 0),
        )
        for o in orders
    ]
    return Page(items=items, limit=page.limit, offset=page.offset, total=total)


@router.get("/{user_id}/reviews", response_model=Page[ReviewOut])
async def list_user_reviews(
    user_id: int,
    db: DbSession,
    page: PageParams,
) -> Page[ReviewOut]:
    """Reviews written by a user. Control endpoint.

    Filters on reviews.author_id, which is indexed, and eager-loads the author
    in one extra round trip.
    """
    stmt = (
        select(Review)
        .options(selectinload(Review.author))
        .where(Review.author_id == user_id)
        .order_by(Review.created_at.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    reviews = list((await db.execute(stmt)).scalars().all())
    items = [
        ReviewOut(
            id=r.id,
            product_id=r.product_id,
            rating=r.rating,
            title=r.title,
            body=r.body,
            created_at=r.created_at,
            author=UserSummary.model_validate(r.author) if r.author else None,
        )
        for r in reviews
    ]
    return Page(items=items, limit=page.limit, offset=page.offset, total=None)
