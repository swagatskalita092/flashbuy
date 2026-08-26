"""Checkout: reserve one unit under a row lock, without double-charging retries."""

from datetime import datetime, timedelta, timezone
import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Order, Product
from app.schemas import CheckoutRequest, OrderOut

router = APIRouter(tags=["checkout"])

# Five minutes is long enough to tap through a fake payment, short enough that
# abandoned holds recycle during a real flash window. Tests can shrink this.
RESERVATION_TTL_SECONDS = int(os.getenv("RESERVATION_TTL_SECONDS", "300"))


async def _get_order_by_idempotency_key(
    db: AsyncSession, idempotency_key: str
) -> Order | None:
    """Find an existing attempt so we can replay it instead of buying twice."""
    result = await db.execute(
        select(Order).where(Order.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


@router.post("/checkout", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def checkout(
    payload: CheckoutRequest, db: AsyncSession = Depends(get_db)
) -> Order:
    """Reserve one unit if stock remains.

    Why idempotency: a buyer's client may retry after a timeout without knowing
    whether the first request already reserved a unit. Without a unique key we
    would decrement stock again and they would double-purchase.

    Why SELECT FOR UPDATE: Phase 1 read stock, then wrote an order and
    decremented, with no lock. Two transactions both saw stock=1 and both
    succeeded. `FOR UPDATE` locks the product row so the second concurrent
    transaction waits until the first commits or rolls back. It then reads
    the *new* stock, not the stale snapshot, so only one of them can take
    the last unit.
    """
    existing = await _get_order_by_idempotency_key(db, payload.idempotency_key)
    if existing is not None:
        return existing

    product_result = await db.execute(
        select(Product).where(Product.id == payload.product_id).with_for_update()
    )
    product = product_result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")

    # Re-check after the lock: a twin retry may have committed while we waited.
    existing = await _get_order_by_idempotency_key(db, payload.idempotency_key)
    if existing is not None:
        return existing

    if product.stock <= 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="out of stock")

    now = datetime.now(timezone.utc)
    order = Order(
        product_id=product.id,
        buyer_id=payload.buyer_id,
        status="reserved",
        idempotency_key=payload.idempotency_key,
        expires_at=now + timedelta(seconds=RESERVATION_TTL_SECONDS),
    )
    db.add(order)
    product.stock -= 1
    try:
        await db.commit()
    except IntegrityError:
        # Unique constraint lost a race on the same key. Roll back the extra
        # decrement (it never committed) and return the winner's order.
        await db.rollback()
        replay = await _get_order_by_idempotency_key(db, payload.idempotency_key)
        if replay is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="checkout conflict",
            )
        return replay

    await db.refresh(order)
    return order
