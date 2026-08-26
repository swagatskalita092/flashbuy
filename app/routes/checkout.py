"""Checkout: reserve one unit under a row lock, without double-charging retries."""

from datetime import datetime, timedelta, timezone
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.metrics import (
    checkout_latency_seconds,
    observe_latency,
    record_rejection,
    record_success,
    set_stock,
)
from app.models import Order, Product
from app.redis_client import get_redis
from app.schemas import CheckoutRequest, OrderOut
from app.waiting_room import consume_admission_token, peek_admission_token

router = APIRouter(tags=["checkout"])

# Five minutes is long enough to tap through a fake payment, short enough that
# abandoned holds recycle during a real flash window. Tests can shrink this.
RESERVATION_TTL_SECONDS = int(os.getenv("RESERVATION_TTL_SECONDS", "300"))


TEST_BYPASS_HEADER = "X-FlashBuy-Test-Bypass"


def _allows_test_bypass(request: Request) -> bool:
    """Internal-only shortcut around the waiting room.

    Used by pytest and scripts/prove_race_condition.py so correctness tests
    can hit checkout without standing in line. This is NOT a public API:
    a missing token from a real buyer must 403. Enabled only when the
    documented bypass header is present.
    """
    return request.headers.get(TEST_BYPASS_HEADER) == "1"


async def _get_order_by_idempotency_key(
    db: AsyncSession, idempotency_key: str
) -> Order | None:
    """Find an existing attempt so we can replay it instead of buying twice."""
    result = await db.execute(
        select(Order).where(Order.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


async def _require_admission(payload: CheckoutRequest, request: Request) -> None:
    """Reject checkout unless this buyer was drip-fed a still-valid token.

    Replay of an existing idempotency_key skips this: the first attempt already
    paid the waiting-room cost. New attempts without a token would let everyone
    bypass the valve and stampede Postgres again.
    """
    if _allows_test_bypass(request):
        return
    if not payload.admission_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admission token required; join the waiting room first",
        )
    redis = await get_redis()
    grant = await peek_admission_token(redis, payload.admission_token)
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admission token missing or expired",
        )
    if grant["product_id"] != str(payload.product_id) or grant["buyer_id"] != payload.buyer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admission token does not match this checkout",
        )


@router.post("/checkout", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def checkout(
    payload: CheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
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

    Why the waiting room token: row locks make checkout *correct*, not *cheap*.
    Tokens cap how many lock-taking transactions start per second.
    """
    with observe_latency(checkout_latency_seconds):
        try:
            return await _checkout_body(payload, request, db)
        except HTTPException as exc:
            if exc.status_code == 409:
                reason = (
                    "checkout_conflict"
                    if exc.detail == "checkout conflict"
                    else "out_of_stock"
                )
                record_rejection(reason)
            elif exc.status_code == 403:
                record_rejection("missing_or_expired_admission_token")
            raise


async def _checkout_body(
    payload: CheckoutRequest,
    request: Request,
    db: AsyncSession,
) -> Order:
    """Inner checkout so latency/success metrics wrap every path including errors."""
    existing = await _get_order_by_idempotency_key(db, payload.idempotency_key)
    if existing is not None:
        return existing

    await _require_admission(payload, request)

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
        set_stock(str(product.id), 0)
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
    set_stock(str(product.id), product.stock)
    record_success()
    if payload.admission_token and not _allows_test_bypass(request):
        redis = await get_redis()
        await consume_admission_token(redis, payload.admission_token)
    return order
