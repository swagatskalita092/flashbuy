"""Virtual waiting room: a Redis FIFO that drip-feeds buyers into checkout.

Overall design
--------------
Even with SELECT FOR UPDATE, ten thousand concurrent checkout transactions
still mean ten thousand connections, lock waits, and query planning on
Postgres. Most of those buyers cannot win a unit anyway. The waiting room
moves the stampede to Redis: joining a sorted set is cheap, and a background
loop admits only N buyers per second into the real checkout path.

If the admission rate is **too high**, we are back to slamming Postgres —
locks pile up and checkouts time out, which is the original problem. If it
is **too low**, the sale feels frozen: stock sits unused while people wait,
and impatient clients retry (which is why join is rate-limited). Tune N so
checkout latency stays healthy, not so every ticket is admitted instantly.

Why a sorted set (not a list)
-----------------------------
Each product has a sorted set scored by a monotonic join sequence (INCR).
That is FIFO without depending on wall clocks. ZRANK is O(log N) for "what
place am I?", which a LIST would answer with LPOS (O(N)). Admission is
ZRANGE of the lowest scores, then ZREM — still logarithmic, still atomic
enough per ticket for this demo.
"""

from __future__ import annotations

import json
import os
import uuid

from redis.asyncio import Redis

from app.redis_client import redis_key
from app.metrics import set_queue_depth

ADMISSION_BATCH_SIZE = int(os.getenv("ADMISSION_BATCH_SIZE", "20"))
ADMISSION_TOKEN_TTL_SECONDS = int(os.getenv("ADMISSION_TOKEN_TTL_SECONDS", "120"))
ADMISSION_TICK_SECONDS = float(os.getenv("ADMISSION_TICK_SECONDS", "1"))


def _queue_key(product_id: str) -> str:
    """One FIFO per product so a hot SKU does not starve behind another sale."""
    return redis_key("wait", "q", product_id)


def _ticket_key(ticket_id: str) -> str:
    """Ticket metadata lives outside the set so status still works after admission."""
    return redis_key("wait", "ticket", ticket_id)


def _token_key(token: str) -> str:
    """The capability checkout actually checks; TTL is the 'complete it now' window."""
    return redis_key("wait", "token", token)


def _products_key() -> str:
    """Set of product ids that currently have waiters, so the sweeper need not SCAN."""
    return redis_key("wait", "products")


def _seq_key(product_id: str) -> str:
    """Monotonic score source — safer FIFO than timestamp if two joins share a ms."""
    return redis_key("wait", "seq", product_id)


async def join_queue(redis: Redis, product_id: str, buyer_id: str) -> dict:
    """Enqueue a buyer and return ticket_id plus 1-based position.

    A new ticket is always created. Rate limiting, not this function, is what
    stops someone from buying every slot in line. Position is ZRANK+1 after
    ZADD so the caller sees the line including themselves.
    """
    ticket_id = str(uuid.uuid4())
    score = await redis.incr(_seq_key(product_id))
    mapping = {
        "ticket_id": ticket_id,
        "product_id": product_id,
        "buyer_id": buyer_id,
        "status": "waiting",
    }
    pipe = redis.pipeline()
    pipe.hset(_ticket_key(ticket_id), mapping=mapping)
    pipe.zadd(_queue_key(product_id), {ticket_id: score})
    pipe.sadd(_products_key(), product_id)
    await pipe.execute()
    rank = await redis.zrank(_queue_key(product_id), ticket_id)
    position = (rank + 1) if rank is not None else 1
    depth = await redis.zcard(_queue_key(product_id))
    set_queue_depth(product_id, int(depth))
    return {
        "ticket_id": ticket_id,
        "product_id": product_id,
        "buyer_id": buyer_id,
        "position": position,
        "admitted": False,
        "admission_token": None,
    }


async def get_ticket_status(redis: Redis, ticket_id: str) -> dict | None:
    """Current place in line, or admission token if the drip-feed already picked them.

    Position is recomputed from the live set so we do not store a stale
    'you are #3' while two people ahead were admitted.
    """
    data = await redis.hgetall(_ticket_key(ticket_id))
    if not data:
        return None

    product_id = data["product_id"]
    token = data.get("admission_token") or None
    admitted = data.get("status") == "admitted"
    if admitted and token:
        still_valid = await redis.exists(_token_key(token))
        if not still_valid:
            return {
                "ticket_id": ticket_id,
                "product_id": product_id,
                "buyer_id": data["buyer_id"],
                "position": None,
                "admitted": False,
                "admission_token": None,
                "admission_expired": True,
            }
        return {
            "ticket_id": ticket_id,
            "product_id": product_id,
            "buyer_id": data["buyer_id"],
            "position": 0,
            "admitted": True,
            "admission_token": token,
            "admission_expired": False,
        }

    rank = await redis.zrank(_queue_key(product_id), ticket_id)
    position = (rank + 1) if rank is not None else None
    return {
        "ticket_id": ticket_id,
        "product_id": product_id,
        "buyer_id": data["buyer_id"],
        "position": position,
        "admitted": False,
        "admission_token": None,
        "admission_expired": False,
    }


async def admit_waiting_buyers(redis: Redis, batch_size: int | None = None) -> int:
    """Pop up to N waiters per product and hand each a short-lived token.

    N is a valve on Postgres load, not a fairness tweak: each admitted buyer
    is about to run SELECT FOR UPDATE. Tokens expire so someone who walks
    away does not occupy a checkout slot forever; they must re-join.
    """
    n = batch_size if batch_size is not None else ADMISSION_BATCH_SIZE
    product_ids = await redis.smembers(_products_key())
    admitted = 0
    for product_id in product_ids:
        ticket_ids = await redis.zrange(_queue_key(product_id), 0, n - 1)
        for ticket_id in ticket_ids:
            token = str(uuid.uuid4())
            payload = await redis.hgetall(_ticket_key(ticket_id))
            if not payload:
                await redis.zrem(_queue_key(product_id), ticket_id)
                continue
            token_body = json.dumps(
                {
                    "ticket_id": ticket_id,
                    "product_id": payload["product_id"],
                    "buyer_id": payload["buyer_id"],
                }
            )
            pipe = redis.pipeline()
            pipe.set(_token_key(token), token_body, ex=ADMISSION_TOKEN_TTL_SECONDS)
            pipe.hset(
                _ticket_key(ticket_id),
                mapping={"status": "admitted", "admission_token": token},
            )
            pipe.zrem(_queue_key(product_id), ticket_id)
            await pipe.execute()
            admitted += 1
        remaining = await redis.zcard(_queue_key(product_id))
        set_queue_depth(product_id, int(remaining))
        if remaining == 0:
            await redis.srem(_products_key(), product_id)
    return admitted


async def peek_admission_token(redis: Redis, token: str) -> dict | None:
    """Read a token without consuming it (checkout retries before a successful reserve)."""
    raw = await redis.get(_token_key(token))
    if not raw:
        return None
    return json.loads(raw)


async def consume_admission_token(redis: Redis, token: str) -> None:
    """Burn the token after a successful reserve so one admission cannot buy twice."""
    await redis.delete(_token_key(token))
