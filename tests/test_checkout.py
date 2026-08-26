"""Checkout, concurrency, idempotency, and reservation-expiry behaviour."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Order
from app.reservations import expire_reservations
from tests.conftest import unique_key

pytestmark = pytest.mark.asyncio


async def _create_product(client, stock: int):
    """Helper: one product with a known stock so assertions stay obvious."""
    response = await client.post(
        "/products",
        json={"name": "Test Widget", "stock": stock, "price_cents": 999},
    )
    assert response.status_code == 201
    return response.json()


async def test_successful_checkout_reduces_stock_by_one(client):
    """Happy path still takes exactly one unit; status is reserved, not paid."""
    product = await _create_product(client, stock=5)

    checkout = await client.post(
        "/checkout",
        json={
            "product_id": product["id"],
            "buyer_id": "buyer-1",
            "idempotency_key": unique_key(),
        },
    )
    assert checkout.status_code == 201
    body = checkout.json()
    assert body["buyer_id"] == "buyer-1"
    assert body["status"] == "reserved"
    assert body["product_id"] == product["id"]

    fetched = await client.get(f"/products/{product['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["stock"] == 4


async def test_checkout_on_zero_stock_returns_409(client):
    """Empty shelf must fail closed even with a unique idempotency key."""
    product = await _create_product(client, stock=0)

    checkout = await client.post(
        "/checkout",
        json={
            "product_id": product["id"],
            "buyer_id": "buyer-1",
            "idempotency_key": unique_key(),
        },
    )
    assert checkout.status_code == 409
    assert checkout.json()["detail"] == "out of stock"

    fetched = await client.get(f"/products/{product['id']}")
    assert fetched.json()["stock"] == 0


async def test_create_and_get_product(client):
    """Catalog round-trip is unchanged in Phase 2."""
    created = await _create_product(client, stock=10)
    fetched = await client.get(f"/products/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Test Widget"
    assert fetched.json()["stock"] == 10
    assert fetched.json()["price_cents"] == 999


async def test_concurrent_checkout_does_not_oversell(client):
    """Several buyers at once cannot take more units than stock.

    Smaller than the 50-request script so CI stays fast, but still concurrent
    enough that Phase 1's unlocked read would oversell.
    """
    stock = 5
    product = await _create_product(client, stock=stock)
    product_id = product["id"]

    async def buy(i: int):
        return await client.post(
            "/checkout",
            json={
                "product_id": product_id,
                "buyer_id": f"buyer-{i}",
                "idempotency_key": unique_key(),
            },
        )

    responses = await asyncio.gather(*[buy(i) for i in range(20)])
    successes = [r for r in responses if r.status_code == 201]
    conflicts = [r for r in responses if r.status_code == 409]
    assert len(successes) == stock
    assert len(conflicts) == 20 - stock
    fetched = await client.get(f"/products/{product_id}")
    assert fetched.json()["stock"] == 0


async def test_duplicate_idempotency_key_does_not_create_second_order(client):
    """A timeout retry with the same key must replay the first reservation."""
    product = await _create_product(client, stock=3)
    key = unique_key()
    body = {
        "product_id": product["id"],
        "buyer_id": "buyer-1",
        "idempotency_key": key,
    }
    first = await client.post("/checkout", json=body)
    second = await client.post("/checkout", json=body)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    fetched = await client.get(f"/products/{product['id']}")
    assert fetched.json()["stock"] == 2


async def test_expired_reservation_releases_stock(client, session_factory):
    """Abandoned holds must return the unit; otherwise flash stock leaks."""
    product = await _create_product(client, stock=1)
    checkout = await client.post(
        "/checkout",
        json={
            "product_id": product["id"],
            "buyer_id": "buyer-1",
            "idempotency_key": unique_key(),
        },
    )
    assert checkout.status_code == 201
    assert (await client.get(f"/products/{product['id']}")).json()["stock"] == 0

    order_id = checkout.json()["id"]
    async with session_factory() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one()
        order.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
        released = await expire_reservations(session)
        assert released == 1

    fetched = await client.get(f"/products/{product['id']}")
    assert fetched.json()["stock"] == 1

    confirm = await client.post(f"/orders/{order_id}/confirm")
    assert confirm.status_code == 409


async def test_confirm_reserved_order(client):
    """Payment stand-in: reserved becomes confirmed and stock stays deducted."""
    product = await _create_product(client, stock=1)
    checkout = await client.post(
        "/checkout",
        json={
            "product_id": product["id"],
            "buyer_id": "buyer-1",
            "idempotency_key": unique_key(),
        },
    )
    order_id = checkout.json()["id"]
    confirm = await client.post(f"/orders/{order_id}/confirm")
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "confirmed"
    fetched = await client.get(f"/products/{product['id']}")
    assert fetched.json()["stock"] == 0
