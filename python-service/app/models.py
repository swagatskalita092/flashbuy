"""SQLAlchemy models for FlashBuy inventory and orders."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
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


class OutboxEvent(Base):
    """One row per domain event this service has promised to deliver.

    Phase 6 (transactional outbox). The row is inserted in the *same*
    transaction that confirms an order (see app/routes/orders.py), so an
    event can never be lost to a crash between "order confirmed" and
    "event recorded" - either both happen (one commit) or neither does.

    A separate publisher process (app/outbox_publisher.py) is the only
    thing that reads this table for delivery; it sets `published_at` only
    after RabbitMQ has confirmed receipt (publisher confirms), never
    before. Until then a row is retried indefinitely - at-least-once
    delivery, not exactly-once. See docs/ENGINEERING_JOURNAL.md Phase 6.
    """

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
