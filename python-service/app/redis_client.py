"""Redis connection used for flash-sale traffic shaping, not for inventory.

Postgres remains the source of truth for stock and orders. Redis is here for
operations that must stay cheap under a burst of thousands of requests in one
second:

- FIFO waiting-room queues (atomic ZADD / ZRANGE / ZREM)
- Short-lived admission tokens (SET with TTL)
- Token-bucket rate limits (atomic Lua so two parallel joins cannot both
  refill and both consume the last token)

Those are in-memory, O(log N) or better, and do not take a row lock. Using
Postgres for the same jobs would mean extra tables, extra transactions, and
the exact database pile-up the waiting room is meant to prevent.
"""

import os

from redis.asyncio import Redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Prefix every key so tests can FLUSHDB on a dedicated logical DB without
# guessing key names, and so a shared Redis never collides with other apps.
KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "flashbuy:")

# redis-py's asyncio ConnectionPool default is max_connections=2**31 (effectively
# unbounded). In practice the process still opens sockets on demand, and under
# 500 Locust users polling ~1s plus joins and the admission loop we saw bursty
# HTTP 500s on /waiting-room/status. Pinning an explicit pool of 1024:
#   500 concurrent status polls
# + 25/s joins during spawn
# + admission pipeline
# + headroom so we fail with a pool error we can turn into 503, not a surprise
#   OS-level EMFILE. 1024 is well under Redis maxclients (default 10000).
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "1024"))

_redis: Redis | None = None


def redis_key(*parts: str) -> str:
    """Build a namespaced key; never interpolate unsanitized user input as a whole key."""
    return KEY_PREFIX + ":".join(parts)


async def get_redis() -> Redis:
    """Lazy singleton so importing modules does not open a socket at collect time."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            max_connections=REDIS_MAX_CONNECTIONS,
            socket_connect_timeout=2,
            socket_timeout=5,
        )
    return _redis


async def close_redis() -> None:
    """Drop the pool on shutdown so Docker stop does not hang on open clients."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
