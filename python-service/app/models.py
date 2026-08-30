"""SQLAlchemy models for FlashBuy inventory and orders."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Product(Base):
    """A sellable SKU whose `stock` is the remaining units we may still reserve.

    Stock is mutated only while the row is locked (`SELECT FOR UPDATE`) so two
    buyers cannot both decide that the last unit is theirs. We keep the model
    small on purpose: this is a flash-sale demo, not a catalog.
    """

    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    orders: Mapped[list["Order"]] = relationship(back_populates="product")


class Order(Base):
    """A hold on one unit of a product, later confirmed or expired.

    Checkout does not mean 'paid'. It means 'this unit is taken off the shelf
    for a few minutes'. That is why status starts as `reserved` with an
    `expires_at`. If we marked sold at checkout, abandoned carts would
    permanently shrink inventory.

    `idempotency_key` is unique so a client timeout + retry cannot buy twice.
    The database unique constraint is the last line of defense when two retries
    race past the application-level lookup.
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    buyer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="reserved")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    product: Mapped[Product] = relationship(back_populates="orders")
