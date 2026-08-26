"""Orders: the fulfilment queue, order detail, and order creation."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from shop.dependencies import CurrentUser, DbSession, PageParams
from shop.models import ORDER_STATUSES, Order, OrderItem, Product
from shop.schemas import OrderCreate, OrderItemOut, OrderOut, Page

logger = logging.getLogger("shop.orders")

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _item_out(item: OrderItem, product: Product | None) -> OrderItemOut:
    return OrderItemOut(
        id=item.id,
        product_id=item.product_id,
        quantity=item.quantity,
        unit_price_cents=item.unit_price_cents,
        product_title=product.title if product else None,
        product_sku=product.sku if product else None,
    )


@router.get("", response_model=Page[OrderOut])
async def list_orders(
    db: DbSession,
    page: PageParams,
    _user: CurrentUser,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> Page[OrderOut]:
    """The fulfilment queue: recent orders with their line items.

    This is the operations console listing, not a customer's own history -
    a customer reads GET /api/users/{id}/orders. Role-based access control is
    out of scope for the benchmark application; the caller only has to be
    authenticated.

    ---------------------------------------------------------------------
    PLANTED PATHOLOGY P1 - N+1 on order line items.

    The orders themselves are fetched in one indexed query. The line items
    are then fetched one order at a time inside the loop below, producing
    `page.limit` extra round trips with the same SQL fingerprint and a
    different bound parameter each time. This is the flagship case for
    detector D1.

    Every clause of the N+1 definition in DESIGN.md 6.2 is satisfied on
    purpose: the queries are siblings under one request span, they share a
    fingerprint, they run sequentially (there is no gather here), the bound
    parameter varies, and each returns only a handful of rows.

    Do not "fix" this with selectinload. It is planted.
    See PATHOLOGIES.md (P1).
    ---------------------------------------------------------------------
    """
    if status_filter is not None and status_filter not in ORDER_STATUSES:
        raise HTTPException(
            # Literal 422 rather than status.HTTP_422_*: Starlette renamed the
            # constant and deprecated the old spelling, and pinning ourselves to
            # either name would break on one side of that change.
            status_code=422,
            detail=f"status must be one of {', '.join(ORDER_STATUSES)}",
        )

    stmt = select(Order)
    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)
    # Backed by idx_orders_placed_at.
    stmt = (
        stmt.order_by(Order.placed_at.desc(), Order.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    orders = list((await db.execute(stmt)).scalars().all())

    out: list[OrderOut] = []
    for order in orders:
        # One query per order. This is the N+1. The join to products is inside
        # the same statement, so there is exactly one extra round trip per
        # order and therefore exactly one fingerprint to detect.
        item_stmt = (
            select(OrderItem)
            .options(joinedload(OrderItem.product))
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.id)
        )
        items = list((await db.execute(item_stmt)).scalars().all())
        out.append(
            OrderOut(
                id=order.id,
                user_id=order.user_id,
                status=order.status,
                total_cents=order.total_cents,
                currency=order.currency,
                shipping_country=order.shipping_country,
                placed_at=order.placed_at,
                items=[_item_out(i, i.product) for i in items],
            )
        )

    return Page(items=out, limit=page.limit, offset=page.offset, total=None)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: int, db: DbSession) -> OrderOut:
    """Single order with line items and product details.

    Control endpoint. Exactly the same data as one entry of the fulfilment
    queue above, loaded the right way: `selectinload` collapses the line items
    into one additional query regardless of how many there are.
    """
    stmt = (
        select(Order)
        .options(selectinload(Order.items).joinedload(OrderItem.product))
        .where(Order.id == order_id)
    )
    order = (await db.execute(stmt)).unique().scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such order")

    return OrderOut(
        id=order.id,
        user_id=order.user_id,
        status=order.status,
        total_cents=order.total_cents,
        currency=order.currency,
        shipping_country=order.shipping_country,
        placed_at=order.placed_at,
        items=[_item_out(i, i.product) for i in order.items],
    )


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    db: DbSession,
    user: CurrentUser,
) -> OrderOut:
    """Place an order.

    Control endpoint. Every product on the order is loaded in a single query
    rather than one per line, stock is checked before anything is written, and
    the whole thing commits as one transaction.
    """
    wanted = {line.product_id: line.quantity for line in payload.items}
    if len(wanted) != len(payload.items):
        raise HTTPException(
            # Literal 422 rather than status.HTTP_422_*: Starlette renamed the
            # constant and deprecated the old spelling, and pinning ourselves to
            # either name would break on one side of that change.
            status_code=422,
            detail="Duplicate product_id in order",
        )

    product_stmt = select(Product).where(Product.id.in_(list(wanted)))
    products = {p.id: p for p in (await db.execute(product_stmt)).scalars().all()}

    missing = sorted(set(wanted) - set(products))
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown product_id(s): {missing}",
        )

    for product_id, quantity in wanted.items():
        product = products[product_id]
        if not product.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Product {product_id} is not available",
            )
        if product.stock_qty < quantity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Insufficient stock for product {product_id}: "
                    f"requested {quantity}, available {product.stock_qty}"
                ),
            )

    order = Order(
        user_id=user.id,
        status="pending",
        currency="USD",
        shipping_country=payload.shipping_country,
        total_cents=0,
    )
    db.add(order)
    await db.flush()  # assign order.id without committing

    total = 0
    for product_id, quantity in wanted.items():
        product = products[product_id]
        line_total = product.price_cents * quantity
        total += line_total
        product.stock_qty -= quantity
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product_id,
                quantity=quantity,
                unit_price_cents=product.price_cents,
            )
        )

    order.total_cents = total
    await db.commit()

    logger.info(
        "order created",
        extra={
            "order_id": order.id,
            "user_id": user.id,
            "line_count": len(wanted),
            "total_cents": total,
        },
    )

    return await get_order(order.id, db)
