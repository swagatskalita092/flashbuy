"""Single-leader admission so extra app processes cannot 3x the drip rate.

Naive scale-out would start the FastAPI lifespan on every replica. Each replica
would run admit_waiting_buyers every tick, so three containers would admit
60 buyers/s instead of 20. The waiting room would stop being a valve.

The lock is a Redis key held with SET NX + TTL. Only the holder runs admission.
It refreshes the TTL while alive. If it dies, the key expires and another
replica's SET NX succeeds within one TTL (default 5s).
"""

from __future__ import annotations

import os
import socket
import uuid

from redis.asyncio import Redis

from app.redis_client import redis_key

ADMISSION_LEADER_TTL_SECONDS = int(os.getenv("ADMISSION_LEADER_TTL_SECONDS", "5"))
INSTANCE_ID = os.getenv("INSTANCE_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
_RENEW_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("EXPIRE", KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


def leader_lock_key() -> str:
    return redis_key("admission", "leader")


async def hold_admission_leadership(
    redis: Redis,
    instance_id: str | None = None,
    ttl_seconds: int | None = None,
) -> bool:
    """True if this instance should run the admission tick.

    Acquire with SET NX, or renew if we already hold the key. Anyone else
    sees the key taken and skips admit_waiting_buyers for this tick.
    """
    owner = instance_id if instance_id is not None else INSTANCE_ID
    ttl = ADMISSION_LEADER_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    key = leader_lock_key()
    acquired = await redis.set(key, owner, nx=True, ex=ttl)
    if acquired:
        return True
    renewed = await redis.eval(_RENEW_LUA, 1, key, owner, str(ttl))
    return bool(renewed)


async def current_admission_leader(redis: Redis) -> str | None:
    """Who holds the lock, if anyone. Used by /health and chaos checks."""
    value = await redis.get(leader_lock_key())
    if value is None:
        return None
    return value if isinstance(value, str) else value.decode()
