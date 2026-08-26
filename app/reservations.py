"""Release abandoned flash-sale holds so inventory is not lost forever."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.metrics import set_stock
from app.models import Order, Product


async def expire_reservations(session: AsyncSession) -> int:
    """Mark overdue `reserved` orders as `expired` and put their units back.

    Checkout takes stock immediately so two buyers cannot grab the same unit.
    If the buyer never 'pays' (confirm), that unit would sit reserved forever.
    This sweep is the safety net: past `expires_at`, the hold is void and
    `product.stock` goes up by one.

    We `SKIP LOCKED` so a checkout that currently holds the product/order row
    is not blocked, and we lock the product before incrementing so the put-back
    cannot interleave with another decrement on a stale count.
    """
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(Order)
        .where(Order.status == "reserved", Order.expires_at <= now)
        .with_for_update(skip_locked=True)
    )
    expired = list(result.scalars().all())
    for order in expired:
        product_result = await session.execute(
            select(Product).where(Product.id == order.product_id).with_for_update()
        )
        product = product_result.scalar_one()
        order.status = "expired"
        product.stock += 1
        set_stock(str(product.id), product.stock)
    await session.commit()
    return len(expired)
