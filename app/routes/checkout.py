from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Order, Product
from app.schemas import CheckoutRequest, OrderOut

router = APIRouter(tags=["checkout"])


@router.post("/checkout", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def checkout(
    payload: CheckoutRequest, db: AsyncSession = Depends(get_db)
) -> Order:
    # Intentionally naive: read stock, then write order + decrement with no lock.
    # Concurrent checkouts can oversell. Do not add SELECT FOR UPDATE here.
    product = await db.get(Product, payload.product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")

    if product.stock <= 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="out of stock")

    order = Order(
        product_id=product.id,
        buyer_id=payload.buyer_id,
        status="confirmed",
    )
    db.add(order)
    product.stock -= 1
    await db.commit()
    await db.refresh(order)
    return order
