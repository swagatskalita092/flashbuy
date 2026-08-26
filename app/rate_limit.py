"""Token-bucket rate limiter stored in Redis.

A token bucket holds up to `capacity` tokens and refills at a steady rate
(tokens per second). Each allowed request costs one token. If the bucket is
empty, the request is rejected (HTTP 429) instead of being queued.

Why a bucket instead of a fixed counter (e.g. INCR with a 60s TTL):
- A fixed window lets a client fire 5 joins at 00:00:59 and 5 more at
  00:01:00 — a burst of 10 in two seconds. The bucket spreads that: after
  spending 5 tokens they must wait for refill, even across a minute boundary.
- Refill is continuous, so a buyer who waited 12 seconds gets some tokens
  back without waiting for the whole window to reset.

The Lua script is required because GET tokens / compute refill / SET tokens
must be one atomic step. Two parallel /join calls must not both read
"5 tokens" and both succeed.
"""

from __future__ import annotations

import os
import time

from redis.asyncio import Redis

from app.redis_client import redis_key

# Configurable so tests can use tiny buckets without sleeping a full minute.
JOIN_LIMIT_PER_BUYER_PER_MINUTE = int(os.getenv("JOIN_LIMIT_PER_BUYER_PER_MINUTE", "5"))
JOIN_LIMIT_PER_IP_PER_MINUTE = int(os.getenv("JOIN_LIMIT_PER_IP_PER_MINUTE", "20"))

# Keep limiter keys around a bit longer than one refill cycle so an idle
# bucket does not disappear and then look "full" again in a confusing way.
_BUCKET_TTL_SECONDS = 120

_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then
  elapsed = 0
end
tokens = math.min(capacity, tokens + elapsed * refill_per_sec)

local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return allowed
"""


async def consume_token(redis: Redis, bucket_key: str, capacity: int, per_minute: int) -> bool:
    """Try to take one token. True means the request may proceed."""
    refill_per_sec = per_minute / 60.0
    allowed = await redis.eval(
        _TOKEN_BUCKET_LUA,
        1,
        bucket_key,
        str(capacity),
        str(refill_per_sec),
        str(time.time()),
        "1",
        str(_BUCKET_TTL_SECONDS),
    )
    return int(allowed) == 1


async def allow_waiting_room_join(redis: Redis, buyer_id: str, ip_address: str) -> tuple[bool, str]:
    """Enforce both per-buyer and per-IP buckets.

    Per-buyer stops one account from stacking queue slots. Per-IP stops a
    botnet of fresh buyer_ids behind one address. Either empty bucket is a 429.
    """
    buyer_ok = await consume_token(
        redis,
        redis_key("rl", "join", "buyer", buyer_id),
        JOIN_LIMIT_PER_BUYER_PER_MINUTE,
        JOIN_LIMIT_PER_BUYER_PER_MINUTE,
    )
    if not buyer_ok:
        return False, "rate limit exceeded for buyer_id (join attempts)"

    ip_ok = await consume_token(
        redis,
        redis_key("rl", "join", "ip", ip_address),
        JOIN_LIMIT_PER_IP_PER_MINUTE,
        JOIN_LIMIT_PER_IP_PER_MINUTE,
    )
    if not ip_ok:
        return False, "rate limit exceeded for IP address (join attempts)"

    return True, ""
