"""Leader election: only one replica may drip-feed admission."""

import asyncio

import pytest

from app.admission_leader import hold_admission_leadership, leader_lock_key
from app.redis_client import get_redis
from app.waiting_room import admit_waiting_buyers, count_live_admission_tokens

pytestmark = pytest.mark.asyncio


async def _join_many(public_client, product_id: str, n: int) -> None:
    for i in range(n):
        response = await public_client.post(
            "/waiting-room/join",
            json={"product_id": product_id, "buyer_id": f"elect-{i}"},
            headers={"X-Forwarded-For": f"198.51.100.{i + 1}"},
        )
        assert response.status_code == 200


async def _loop(
    instance_id: str,
    stop: asyncio.Event,
    admitted: dict[str, int],
    ttl: int,
) -> None:
    redis = await get_redis()
    while not stop.is_set():
        if await hold_admission_leadership(redis, instance_id, ttl_seconds=ttl):
            admitted[instance_id] += await admit_waiting_buyers(redis, batch_size=5)
        await asyncio.sleep(0.05)


async def test_three_admission_loops_do_not_triple_the_drip(public_client):
    """Three concurrent loops must admit at one replica's rate, not 3x.

    Without the Redis lock each loop would pop ADMISSION_BATCH_SIZE per tick.
    """
    product = await public_client.post(
        "/products",
        json={"name": "Election Widget", "stock": 80, "price_cents": 100},
    )
    assert product.status_code == 201
    product_id = product.json()["id"]
    await _join_many(public_client, product_id, 40)

    redis = await get_redis()
    await redis.delete(leader_lock_key())

    admitted = {"a": 0, "b": 0, "c": 0}
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(_loop(name, stop, admitted, ttl=2))
        for name in admitted
    ]
    await asyncio.sleep(0.85)
    stop.set()
    for task in tasks:
        await task

    live = await count_live_admission_tokens(redis)
    total = sum(admitted.values())
    assert total == live
    winners = [name for name, count in admitted.items() if count > 0]
    assert len(winners) == 1, admitted
    assert admitted[winners[0]] == 40


async def test_admission_leadership_moves_after_lock_expires(public_client):
    """When the leader stops renewing, another instance takes the lock."""
    product = await public_client.post(
        "/products",
        json={"name": "Failover Widget", "stock": 20, "price_cents": 100},
    )
    product_id = product.json()["id"]
    await _join_many(public_client, product_id, 10)

    redis = await get_redis()
    await redis.delete(leader_lock_key())
    ttl = 1

    assert await hold_admission_leadership(redis, "old-leader", ttl_seconds=ttl)
    first = await admit_waiting_buyers(redis, batch_size=3)
    assert first == 3
    assert not await hold_admission_leadership(redis, "challenger", ttl_seconds=ttl)

    await asyncio.sleep(ttl + 0.3)
    assert await hold_admission_leadership(redis, "challenger", ttl_seconds=ttl)
    second = await admit_waiting_buyers(redis, batch_size=3)
    assert second == 3
