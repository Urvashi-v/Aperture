"""Checkout: take a pending order to paid."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from shop.dependencies import CurrentUser, DbSession
from shop.models import Order, OrderItem
from shop.routers.orders import _item_out
from shop.schemas import OrderOut

logger = logging.getLogger("shop.checkout")

router = APIRouter(prefix="/api", tags=["checkout"])


class CheckoutRequest(BaseModel):
    order_id: int
    payment_token: str = "tok_benchmark"


@router.post("/checkout", response_model=OrderOut)
async def checkout(
    payload: CheckoutRequest,
    db: DbSession,
    user: CurrentUser,
) -> OrderOut:
    """Pay for a pending order.

    Today this is the database half of checkout only: ownership and state are
    validated, and the order moves pending -> paid in one transaction. It is
    complete and correct as far as it goes.

    PLANTED PATHOLOGY P6 (three serial external calls) is NOT here yet. It
    needs real partner services to call - payment authorisation, tax
    calculation, and a shipping quote - and inventing fake ones that return
    canned responses would make the serial-await detector meaningless, since
    there would be no real I/O to overlap. The partner services and the serial
    calls land on the day scheduled in PATHOLOGIES.md (P6); until then this
    endpoint is honestly a control.
    """
    stmt = (
        select(Order)
        .options(selectinload(Order.items).joinedload(OrderItem.product))
        .where(Order.id == payload.order_id)
    )
    order = (await db.execute(stmt)).unique().scalar_one_or_none()

    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such order")
    if order.user_id != user.id:
        # 404 rather than 403: do not confirm that someone else's order exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such order")
    if order.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order is {order.status}, not pending",
        )
    if not order.items:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Order has no line items"
        )

    order.status = "paid"
    await db.commit()

    logger.info(
        "order paid",
        extra={
            "order_id": order.id,
            "user_id": user.id,
            "total_cents": order.total_cents,
        },
    )

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
