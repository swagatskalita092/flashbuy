"""Ensure a known product exists so docker-compose is immediately load-testable."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product

SEED_PRODUCT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
SEED_PRODUCT_NAME = "Flash Deal Widget"
SEED_STOCK = 500
SEED_PRICE_CENTS = 1999


async def seed_default_product(session: AsyncSession) -> Product:
    """Insert the demo SKU once. Re-runs must not reset stock to 500."""
    result = await session.execute(select(Product).where(Product.id == SEED_PRODUCT_ID))
    product = result.scalar_one_or_none()
    if product is not None:
        return product

    product = Product(
        id=SEED_PRODUCT_ID,
        name=SEED_PRODUCT_NAME,
        stock=SEED_STOCK,
        price_cents=SEED_PRICE_CENTS,
    )
    session.add(product)
    await session.commit()
    await session.refresh(product)
    return product
