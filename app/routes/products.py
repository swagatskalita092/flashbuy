"""HTTP routes for creating products and reading live stock."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Product
from app.schemas import ProductCreate, ProductOut

router = APIRouter(prefix="/products", tags=["products"])


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate, db: AsyncSession = Depends(get_db)
) -> Product:
    """Insert a SKU. Tests and the race script use this to start from known stock."""
    product = Product(
        name=payload.name,
        stock=payload.stock,
        price_cents=payload.price_cents,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Product:
    """Return current stock so clients (and the race probe) can audit inventory."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    return product
