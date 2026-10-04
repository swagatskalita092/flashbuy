"""Single-leader admission so extra app processes cannot 3x the drip rate.

Naive scale-out would start the FastAPI lifespan on every replica. Each replica
would run admit_waiting_buyers every tick, so three containers would admit
60 buyers/s instead of 20. The waiting room would stop being a valve.

The lock is a Redis key held with SET NX + TTL. Only the holder runs admission.
It refreshes the TTL while alive. If it dies, the key expires and another
replica's SET NX succeeds within one TTL (default 5s).

FENCING TOKEN (added after a real gap was reported in outside feedback):
a lease alone is not enough. Checking "am I leader" and then separately
writing an admission are two different Redis round trips. If this process
freezes (kill -STOP, a GC pause, a frozen VM) in between those two calls,
the TTL can expire and another replica can take over while this one is
paused. When the frozen process wakes up, it has already passed its
check and has no way to know it is stale, so it can go ahead and admit
anyway. Two "leaders" admitting in the same window.

The fix is a fencing token: an epoch number that increases every time the
lock changes hands (not on renewal, only on a fresh acquisition). Every
write to the waiting room must carry the epoch it believed was current at
check time, and the write itself re-checks that epoch, atomically, in the
same Redis call that performs the write. A stale epoch means the write is
refused, no matter how confident the caller feels.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from app.redis_client import redis_key

ADMISSION_LEADER_TTL_SECONDS = int(os.getenv("ADMISSION_LEADER_TTL_SECONDS", "5"))
INSTANCE_ID = os.getenv("INSTANCE_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def leader_lock_key() -> str:
    return redis_key("admission", "leader")


def leader_epoch_key() -> str:
    """A separate key: a monotonically increasing counter that never expires
    on its own.

    This is what makes the token a real fencing token. The lock key can
    expire and be re-acquired any number of times, but the epoch key only
    ever goes up, so a stale epoch from a frozen process can never be
    mistaken for a current one.
    """
    return redis_key("admission", "leader_epoch")


@dataclass(frozen=True)
class LeadershipResult:
    """What a leadership check tells the caller.

    is_leader: whether this instance should run admission this tick.
    epoch: the epoch to pass into the actual admission write. Only
        meaningful when is_leader is True. Never reuse an epoch from an
        earlier tick, always use the one returned by this tick's check.
    """

    is_leader: bool
    epoch: int | None


# Acquire-or-renew, with a fencing epoch, done atomically in one Redis call
# so there is no gap between "check" and "act" at the lock level itself.
#
# KEYS[1] = leader lock key (the lease, expires)
# KEYS[2] = leader epoch key (the fencing counter, never expires)
# ARGV[1] = this instance's id
# ARGV[2] = lease TTL in seconds
#
# Returns the current epoch as a string if this instance holds (or just
# took) leadership this tick, or false if someone else holds it.
_ACQUIRE_OR_RENEW_WITH_EPOCH_LUA = """
local lock_key = KEYS[1]
local epoch_key = KEYS[2]
local owner = ARGV[1]
local ttl = tonumber(ARGV[2])

local current_owner = redis.call("GET", lock_key)

if current_owner == false then
  -- Nobody holds the lease right now: take it, and bump the epoch.
  -- This is the only branch that increments the epoch, a renewal never does.
  redis.call("SET", lock_key, owner, "EX", ttl)
  local new_epoch = redis.call("INCR", epoch_key)
  return tostring(new_epoch)
end

if current_owner == owner then
  -- We already hold it: just extend the lease, epoch stays the same.
  redis.call("EXPIRE", lock_key, ttl)
  local epoch = redis.call("GET", epoch_key)
  return epoch
end

-- Someone else holds it.
return false
"""


async def hold_admission_leadership(
    redis: Redis,
    instance_id: str | None = None,
    ttl_seconds: int | None = None,
) -> LeadershipResult:
    """Check (and if possible take or renew) admission leadership for this tick.

    Returns the current fencing epoch alongside the yes/no answer. Callers
    must pass that exact epoch into admit_waiting_buyers, never a cached
    one from an earlier tick. That is what makes a frozen-then-resumed
    process safe: its old epoch will no longer match by the time it wakes
    up and tries to use it.
    """
    owner = instance_id if instance_id is not None else INSTANCE_ID
    ttl = ADMISSION_LEADER_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    result = await redis.eval(
        _ACQUIRE_OR_RENEW_WITH_EPOCH_LUA,
        2,
        leader_lock_key(),
        leader_epoch_key(),
        owner,
        str(ttl),
    )
    if result is False or result is None:
        return LeadershipResult(is_leader=False, epoch=None)
    epoch = int(result if isinstance(result, (str, int)) else result.decode())
    return LeadershipResult(is_leader=True, epoch=epoch)


async def current_admission_leader(redis: Redis) -> str | None:
    """Who holds the lock, if anyone. Used by /health and chaos checks."""
    value = await redis.get(leader_lock_key())
    if value is None:
        return None
    return value if isinstance(value, str) else value.decode()


async def current_admission_epoch(redis: Redis) -> int | None:
    """The current fencing epoch, if one has ever been issued.

    Used by /health for visibility, and by tests that need to simulate a
    leadership change happening out from under a paused process.
    """
    value = await redis.get(leader_epoch_key())
    if value is None:
        return None
    return int(value if isinstance(value, (str, int)) else value.decode())
