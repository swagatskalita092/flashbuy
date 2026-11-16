"""Order confirmation: the stand-in for 'payment succeeded'."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import OutboxEvent, Order
from app.schemas import OrderOut

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("/{order_id}/confirm", response_model=OrderOut)
async def confirm_order(
    order_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Order:
    """Move `reserved` → `confirmed` without touching stock.

    Stock already left the shelf at checkout. Confirming only means the hold
    should no longer expire. We lock the order row so a sweeper cannot expire
    the same reservation in the same instant we confirm it.

    Phase 6: this is also the one and only place an `order.confirmed`
    outbox row is written, in the *same* transaction as the status flip.
    Either both the status change and the event record commit together, or
    neither does - there is no window where an order is confirmed but no
    event is queued, and no window where an event exists for an order that
    was never actually confirmed (both would be the classic "dual-write"
    bug this phase exists to rule out). A separate publisher process
    delivers the row to RabbitMQ later; this endpoint never talks to the
    broker directly, so a broker outage cannot make confirmation slower or
    fail here.
    """
    result = await db.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order not found")

    if order.status == "confirmed":
        return order

    if order.status != "reserved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"order cannot be confirmed from status {order.status}",
        )

    order.status = "confirmed"
    db.add(
        OutboxEvent(
            event_type="order.confirmed",
            order_id=order.id,
            payload={
                "order_id": str(order.id),
                "product_id": str(order.product_id),
                "buyer_id": order.buyer_id,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    await db.commit()
    await db.refresh(order)
    return order
