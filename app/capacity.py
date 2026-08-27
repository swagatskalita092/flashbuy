"""Map backend capacity failures to HTTP 503 instead of an unhandled 500.

A bare 500 means "our code crashed." Pool exhaustion, Redis timeouts, and
Postgres refusing connections are temporary: if the client retries after a
short wait, the request may succeed. 503 Service Unavailable is the status
that signals that, which is why load balancers and Locust should treat it
differently from a bug.
"""

from fastapi import HTTPException, status
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError

# ConnectionError here is Redis's, which includes "Too many connections" when
# the asyncio pool is full. OSError covers resets/disconnects during a poll burst.
_TRANSIENT = (
    RedisConnectionError,
    RedisTimeoutError,
    SAPoolTimeoutError,
    OperationalError,
    TimeoutError,
    OSError,
    ConnectionError,
)


def is_transient_backend_error(exc: BaseException) -> bool:
    """True when retrying later is more honest than treating this as a logic bug."""
    return isinstance(exc, _TRANSIENT)


def service_unavailable(exc: BaseException) -> HTTPException:
    """Build the 503 the client should see when we are at capacity."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="temporarily unavailable: connection pool exhausted or backend timeout",
    )
