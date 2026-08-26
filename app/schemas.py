import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    stock: int = Field(ge=0)
    price_cents: int = Field(ge=0)


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    stock: int
    price_cents: int

    model_config = {"from_attributes": True}


class CheckoutRequest(BaseModel):
    product_id: uuid.UUID
    buyer_id: str = Field(min_length=1, max_length=255)


class OrderOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    buyer_id: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
