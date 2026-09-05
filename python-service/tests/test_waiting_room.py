"""Waiting room, drip-feed admission, checkout gate, and join rate limits."""

import asyncio
import json

import pytest

from app.rate_limit import JOIN_LIMIT_PER_BUYER_PER_MINUTE
from app.redis_client import get_redis
from app.waiting_room import admit_waiting_buyers
from tests.conftest import unique_key

pytestmark = pytest.mark.asyncio


async def _create_product(client, stock: int = 10):
    """Catalog helper shared with waiting-room tests (bypass header is irrelevant)."""
    response = await client.post(
        "/products",
        json={"name": "Queue Widget", "stock": stock, "price_cents": 500},
    )
    assert response.status_code == 201
    return response.json()


async def test_join_assigns_increasing_positions(public_client):
    """FIFO: the third joiner must see position 3, not a random or colliding slot."""
    product = await _create_product(public_client)
    positions = []
    for i in range(3):
        response = await public_client.post(
            "/waiting-room/join",
            json={"product_id": product["id"], "buyer_id": f"line-buyer-{i}"},
        )
        assert response.status_code == 200
        body = response.json()
        positions.append(body["position"])
        assert body["admitted"] is False
        assert body["admission_token"] is None
    assert positions == [1, 2, 3]


async def test_admission_admits_buyers_over_multiple_ticks(public_client):
    """Each tick only opens the valve a little — two ticks of size 2 admit 4 of 5."""
    product = await _create_product(public_client)
    tickets = []
    for i in range(5):
        response = await public_client.post(
            "/waiting-room/join",
            json={"product_id": product["id"], "buyer_id": f"drip-buyer-{i}"},
        )
        tickets.append(response.json()["ticket_id"])

    redis = await get_redis()
    first = await admit_waiting_buyers(redis, batch_size=2)
    assert first == 2
    admitted_after_one = []
    waiting_after_one = []
    for ticket_id in tickets:
        status = await public_client.get(f"/waiting-room/status/{ticket_id}")
        body = status.json()
        if body["admitted"]:
            admitted_after_one.append(ticket_id)
            assert body["admission_token"]
            assert body["position"] == 0
        else:
            waiting_after_one.append(ticket_id)
    assert len(admitted_after_one) == 2
    assert len(waiting_after_one) == 3

    second = await admit_waiting_buyers(redis, batch_size=2)
    assert second == 2
    still_waiting = 0
    admitted = 0
    for ticket_id in tickets:
        body = (await public_client.get(f"/waiting-room/status/{ticket_id}")).json()
        if body["admitted"]:
            admitted += 1
        else:
            still_waiting += 1
    assert admitted == 4
    assert still_waiting == 1


async def test_count_live_admission_tokens_matches_unconsumed_grants(public_client):
    """SCAN of wait:token:* is what the outstanding-token gauge is based on."""
    from app.waiting_room import count_live_admission_tokens

    product = await _create_product(public_client)
    for i in range(3):
        await public_client.post(
            "/waiting-room/join",
            json={"product_id": product["id"], "buyer_id": f"token-count-{i}"},
        )
    redis = await get_redis()
    granted = await admit_waiting_buyers(redis, batch_size=3)
    assert granted == 3
    assert await count_live_admission_tokens(redis) == 3


async def test_checkout_rejected_without_admission_token(public_client):
    """Unadmitted traffic must not take a product row lock."""
    product = await _create_product(public_client)
    response = await public_client.post(
        "/checkout",
        json={
            "product_id": product["id"],
            "buyer_id": "sneaky",
            "idempotency_key": unique_key(),
        },
    )
    assert response.status_code == 403
    fetched = await public_client.get(f"/products/{product['id']}")
    assert fetched.json()["stock"] == 10


async def test_checkout_succeeds_with_admission_token(public_client):
    """Happy path through the room: join → admit → checkout with the issued token."""
    product = await _create_product(public_client, stock=2)
    join = await public_client.post(
        "/waiting-room/join",
        json={"product_id": product["id"], "buyer_id": "admitted-buyer"},
    )
    ticket_id = join.json()["ticket_id"]
    redis = await get_redis()
    await admit_waiting_buyers(redis, batch_size=1)
    status = await public_client.get(f"/waiting-room/status/{ticket_id}")
    token = status.json()["admission_token"]
    assert token
    checkout = await public_client.post(
        "/checkout",
        json={
            "product_id": product["id"],
            "buyer_id": "admitted-buyer",
            "idempotency_key": unique_key(),
            "admission_token": token,
        },
    )
    assert checkout.status_code == 201
    assert checkout.json()["status"] == "reserved"


async def test_rate_limit_kicks_in_after_configured_join_attempts(public_client):
    """Same buyer_id cannot mint unlimited tickets; the N+1st join is 429."""
    product = await _create_product(public_client)
    buyer_id = "spammer"
    statuses = []
    for i in range(JOIN_LIMIT_PER_BUYER_PER_MINUTE + 1):
        response = await public_client.post(
            "/waiting-room/join",
            json={"product_id": product["id"], "buyer_id": buyer_id},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        )
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert "buyer_id" in response.json()["detail"]
    assert statuses[:JOIN_LIMIT_PER_BUYER_PER_MINUTE] == [200] * JOIN_LIMIT_PER_BUYER_PER_MINUTE
    assert statuses[-1] == 429


async def test_stream_emits_immediately_when_already_admitted(public_client):
    """Admit first, then open SSE: must get the token without waiting for PUBLISH.

    Redis pub/sub does not replay. This is the race the stream handler checks
    with get_ticket_status before it subscribes.
    """
    product = await _create_product(public_client)
    join = await public_client.post(
        "/waiting-room/join",
        json={"product_id": product["id"], "buyer_id": "already-in"},
    )
    ticket_id = join.json()["ticket_id"]
    redis = await get_redis()
    await admit_waiting_buyers(redis, batch_size=1)

    chunks = []
    async with public_client.stream(
        "GET", f"/waiting-room/stream/{ticket_id}", timeout=5.0
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        async for text in response.aiter_text():
            chunks.append(text)
            if "admission_token" in "".join(chunks):
                break
    body = "".join(chunks)
    assert "event: admission" in body
    data_line = next(line for line in body.splitlines() if line.startswith("data:"))
    payload = json.loads(data_line.removeprefix("data:").strip())
    assert payload["admitted"] is True
    assert payload["admission_token"]
    status = await public_client.get(f"/waiting-room/status/{ticket_id}")
    assert status.json()["admission_token"] == payload["admission_token"]


async def test_stream_receives_admission_via_pubsub(public_client):
    """Subscribe first, then admit: the event must arrive from PUBLISH."""
    product = await _create_product(public_client)
    join = await public_client.post(
        "/waiting-room/join",
        json={"product_id": product["id"], "buyer_id": "live-push"},
    )
    ticket_id = join.json()["ticket_id"]

    async def read_event() -> dict:
        async with public_client.stream(
            "GET", f"/waiting-room/stream/{ticket_id}", timeout=8.0
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    return json.loads(line.removeprefix("data:").strip())
        raise AssertionError("stream closed without an admission event")

    reader = asyncio.create_task(read_event())
    await asyncio.sleep(0.4)
    redis = await get_redis()
    assert await admit_waiting_buyers(redis, batch_size=1) == 1
    payload = await asyncio.wait_for(reader, timeout=6)
    assert payload["admitted"] is True
    assert payload["admission_token"]
    status = await public_client.get(f"/waiting-room/status/{ticket_id}")
    assert status.json()["admission_token"] == payload["admission_token"]


async def test_status_returns_503_when_redis_is_unavailable(public_client, monkeypatch):
    """Pool or transport failures must be 503, not an unhandled 500."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app.routes import waiting_room as waiting_room_routes

    async def boom(_redis, _ticket_id):
        raise RedisConnectionError("Too many connections")

    monkeypatch.setattr(waiting_room_routes, "get_ticket_status", boom)
    response = await public_client.get(
        "/waiting-room/status/00000000-0000-4000-8000-000000000099"
    )
    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]
