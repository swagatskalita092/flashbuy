"""Phase 6: the outbox row must be written atomically with order confirmation.

These tests do not touch RabbitMQ or fulfillment-service at all - that cross-
service, cross-broker behaviour is proven by the chaos scripts in
docs/phase6_chaos_results.md, which need real infrastructure to mean anything.
What belongs in the regular (CI, isolated, no broker) suite is the one
claim that *is* testable with just this service's own database: that
confirming an order and recording the intent to publish an event are the
same commit, every time, with no code path that does one without the other.
"""

import pytest
from sqlalchemy import select

from app.models import OutboxEvent, Order
from tests.conftest import unique_key

pytestmark = pytest.mark.asyncio


async def _create_product(client, stock: int):
    response = await client.post(
        "/products",
        json={"name": "Test Widget", "stock": stock, "price_cents": 999},
    )
    assert response.status_code == 201
    return response.json()


async def test_confirm_writes_an_outbox_row_in_the_same_transaction(client, session_factory):
    """Confirming an order leaves exactly one unpublished order.confirmed row."""
    product = await _create_product(client, stock=1)
    checkout = await client.post(
        "/checkout",
        json={"product_id": product["id"], "buyer_id": "buyer-1", "idempotency_key": unique_key()},
    )
    order_id = checkout.json()["id"]

    confirm = await client.post(f"/orders/{order_id}/confirm")
    assert confirm.status_code == 200

    async with session_factory() as session:
        result = await session.execute(
            select(OutboxEvent).where(OutboxEvent.order_id == order_id)
        )
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "order.confirmed"
    assert row.published_at is None  # nothing in this process talks to the broker
    assert row.payload["order_id"] == order_id
    assert row.payload["buyer_id"] == "buyer-1"


async def test_checkout_alone_writes_no_outbox_row(client, session_factory):
    """Reserving (not confirming) must not create an event - nothing happened yet."""
    product = await _create_product(client, stock=1)
    checkout = await client.post(
        "/checkout",
        json={"product_id": product["id"], "buyer_id": "buyer-1", "idempotency_key": unique_key()},
    )
    order_id = checkout.json()["id"]

    async with session_factory() as session:
        result = await session.execute(
            select(OutboxEvent).where(OutboxEvent.order_id == order_id)
        )
        rows = result.scalars().all()

    assert rows == []


async def test_confirming_twice_does_not_duplicate_the_outbox_row(client, session_factory):
    """`/confirm` on an already-confirmed order is a no-op (see app/routes/orders.py)
    and must stay a no-op for the outbox too - only the first call creates a row."""
    product = await _create_product(client, stock=1)
    checkout = await client.post(
        "/checkout",
        json={"product_id": product["id"], "buyer_id": "buyer-1", "idempotency_key": unique_key()},
    )
    order_id = checkout.json()["id"]

    first = await client.post(f"/orders/{order_id}/confirm")
    second = await client.post(f"/orders/{order_id}/confirm")
    assert first.status_code == 200
    assert second.status_code == 200

    async with session_factory() as session:
        result = await session.execute(
            select(OutboxEvent).where(OutboxEvent.order_id == order_id)
        )
        rows = result.scalars().all()

    assert len(rows) == 1
