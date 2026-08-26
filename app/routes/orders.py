"""Order confirmation: the stand-in for 'payment succeeded'."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Order
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
    await db.commit()
    await db.refresh(order)
    return order
