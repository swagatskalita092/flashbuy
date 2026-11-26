"""Phase 6 follow-up: outbox_events retention must never delete an
unpublished row, regardless of age, and must only delete a published row
once it is older than the cutoff. See app/outbox_retention.py and
docs/failure-modes.md #5 for why this exists.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import OutboxEvent
from app.outbox_retention import delete_published_outbox_events
from tests.conftest import unique_key

pytestmark = pytest.mark.asyncio


async def _create_confirmed_order_with_outbox_row(client) -> str:
    """Returns the order id for a checked-out, confirmed order - the only
    code path that writes a real outbox_events row (see app/routes/orders.py)."""
    product_response = await client.post(
        "/products",
        json={"name": "Test Widget", "stock": 1, "price_cents": 999},
    )
    assert product_response.status_code == 201
    product = product_response.json()

    checkout = await client.post(
        "/checkout",
        json={"product_id": product["id"], "buyer_id": "buyer-1", "idempotency_key": unique_key()},
    )
    assert checkout.status_code == 201
    order_id = checkout.json()["id"]

    confirm = await client.post(f"/orders/{order_id}/confirm")
    assert confirm.status_code == 200
    return order_id


async def _set_outbox_fields(session_factory, order_id, **fields):
    """Load the row and the session that loaded it in the same `async with`,
    so the commit happens on the same session the object is attached to."""
    async with session_factory() as session:
        result = await session.execute(
            select(OutboxEvent).where(OutboxEvent.order_id == order_id)
        )
        row = result.scalar_one()
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _outbox_row_exists(session_factory, order_id) -> bool:
    async with session_factory() as session:
        result = await session.execute(
            select(OutboxEvent).where(OutboxEvent.order_id == order_id)
        )
        return result.scalar_one_or_none() is not None


async def test_deletes_a_published_row_older_than_the_cutoff(client, session_factory):
    order_id = await _create_confirmed_order_with_outbox_row(client)
    await _set_outbox_fields(
        session_factory, order_id,
        published_at=datetime.now(timezone.utc) - timedelta(days=10),
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with session_factory() as session:
        deleted = await delete_published_outbox_events(session, older_than=cutoff)

    assert deleted == 1
    assert await _outbox_row_exists(session_factory, order_id) is False


async def test_keeps_a_published_row_newer_than_the_cutoff(client, session_factory):
    order_id = await _create_confirmed_order_with_outbox_row(client)
    await _set_outbox_fields(
        session_factory, order_id,
        published_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with session_factory() as session:
        deleted = await delete_published_outbox_events(session, older_than=cutoff)

    assert deleted == 0
    assert await _outbox_row_exists(session_factory, order_id) is True


async def test_never_deletes_an_unpublished_row_no_matter_how_old(client, session_factory):
    """The safety invariant: published_at IS NULL must survive, even with a
    cutoff permissive enough to catch anything by age alone. An outbox row
    with no published_at is a promise this service has not kept yet -
    deleting it would silently drop an event, which must never happen."""
    order_id = await _create_confirmed_order_with_outbox_row(client)
    await _set_outbox_fields(
        session_factory, order_id,
        created_at=datetime.now(timezone.utc) - timedelta(days=365),
    )
    # published_at was never touched here, so it is still NULL - the
    # condition this test exists to prove the function respects.

    # A cutoff in the far future would catch any row by age alone if the
    # published_at check were ever removed or weakened.
    permissive_cutoff = datetime.now(timezone.utc) + timedelta(days=365)
    async with session_factory() as session:
        deleted = await delete_published_outbox_events(session, older_than=permissive_cutoff)

    assert deleted == 0
    assert await _outbox_row_exists(session_factory, order_id) is True


async def test_respects_the_batch_limit(client, session_factory):
    order_ids = [await _create_confirmed_order_with_outbox_row(client) for _ in range(3)]
    for order_id in order_ids:
        await _set_outbox_fields(
            session_factory, order_id,
            published_at=datetime.now(timezone.utc) - timedelta(days=10),
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with session_factory() as session:
        deleted = await delete_published_outbox_events(session, older_than=cutoff, limit=2)

    assert deleted == 2  # capped, not all 3 in one statement

    remaining = sum(
        [await _outbox_row_exists(session_factory, order_id) for order_id in order_ids]
    )
    assert remaining == 1
