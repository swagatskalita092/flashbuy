"""Fulfillment Service models: the idempotent inbox side of Phase 6."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class InboxEvent(Base):
    """One row per event this service has ever committed a side effect for.

    `event_id` is the FlashBuy outbox row's id, carried through unchanged.
    Its uniqueness constraint is the entire idempotency mechanism: a
    duplicate delivery (the outbox publisher's own crash window, or a
    network retry) hits this constraint and is detected *in the same
    transaction* that would otherwise create a second fulfillment job -
    so "already processed" and "create the job" can never race each other.
    """

    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_inbox_events_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FulfillmentJob(Base):
    """The actual downstream side effect: 'go fulfill this order.'

    Created at most once per event_id, enforced by InboxEvent's unique
    constraint above, never by anything in this table itself.
    """

    __tablename__ = "fulfillment_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="created")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
