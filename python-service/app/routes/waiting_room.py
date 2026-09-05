"""HTTP API for the virtual waiting room (join, poll, and SSE push)."""

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.capacity import is_transient_backend_error, service_unavailable
from app.db import get_db
from app.models import Product
from app.metrics import (
    join_latency_seconds,
    observe_latency,
    record_rejection,
    sse_connection_closed,
    sse_connection_opened,
)
from app.rate_limit import allow_waiting_room_join
from app.redis_client import get_pubsub_redis, get_redis
from app.schemas import WaitingRoomJoinRequest, WaitingRoomStatusOut
from app.waiting_room import (
    admission_channel,
    admission_event_payload,
    get_ticket_status,
    join_queue,
)

router = APIRouter(prefix="/waiting-room", tags=["waiting-room"])

SSE_HEARTBEAT_SECONDS = 18


def client_ip(request: Request) -> str:
    """Prefer X-Forwarded-For so rate limits still work behind Docker/nginx."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


@router.post("/join", response_model=WaitingRoomStatusOut)
async def join_waiting_room(
    payload: WaitingRoomJoinRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Take a number for this product instead of opening a checkout transaction.

    The expensive work (row locks) happens only after admission. Joining is a
    Redis ZADD plus a rate-limit check so a script cannot occupy the whole line.
    """
    with observe_latency(join_latency_seconds):
        try:
            product = await db.get(Product, payload.product_id)
            if product is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")

            redis = await get_redis()
            allowed, reason = await allow_waiting_room_join(
                redis, payload.buyer_id, client_ip(request)
            )
            if not allowed:
                record_rejection("rate_limited")
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason
                )

            return await join_queue(redis, str(payload.product_id), payload.buyer_id)
        except HTTPException:
            raise
        except Exception as exc:
            if is_transient_backend_error(exc):
                raise service_unavailable(exc) from exc
            raise


@router.get("/status/{ticket_id}", response_model=WaitingRoomStatusOut)
async def waiting_room_status(ticket_id: uuid.UUID) -> dict:
    """Poll place-in-line. Clients should not hammer this; admission is server-side."""
    try:
        redis = await get_redis()
        result = await get_ticket_status(redis, str(ticket_id))
    except Exception as exc:
        if is_transient_backend_error(exc):
            raise service_unavailable(exc) from exc
        raise
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found")
    return result


def _sse_data(payload: str) -> str:
    return f"event: admission\ndata: {payload}\n\n"


@router.get("/stream/{ticket_id}")
async def waiting_room_stream(ticket_id: uuid.UUID, request: Request) -> StreamingResponse:
    """Push admission as one SSE event. Polling GET /status remains the fallback.

    Why this exists: thousands of waiters GETting /status every second each take
    a Redis command connection for a round trip that almost always says "still
    waiting." That exhausted the 1024 command pool (Finding #3). One long-lived
    SUBSCRIBE per ticket waits for PUBLISH instead.
    """
    ticket = str(ticket_id)
    try:
        redis = await get_redis()
        snapshot = await get_ticket_status(redis, ticket)
    except Exception as exc:
        if is_transient_backend_error(exc):
            raise service_unavailable(exc) from exc
        raise
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found")

    async def events():
        # If the ticket is already admitted, return that as one event and close.
        # Without this, a grant that happened in the tiny window before the
        # client subscribed is gone forever: Redis pub/sub does not replay.
        if snapshot.get("admitted") and snapshot.get("admission_token"):
            yield _sse_data(
                admission_event_payload(ticket, snapshot["admission_token"])
            )
            return

        sse_connection_opened()
        pubsub = None
        try:
            pubsub_redis = await get_pubsub_redis()
            pubsub = pubsub_redis.pubsub()
            await pubsub.subscribe(admission_channel(ticket))
            # Subscribe is not instantaneous relative to PUBLISH. Recheck
            # status now that we are subscribed: if admission landed between
            # the first GET and SUBSCRIBE, the message may have been dropped
            # and this read is the only way we still learn about it.
            latest = await get_ticket_status(redis, ticket)
            if latest and latest.get("admitted") and latest.get("admission_token"):
                yield _sse_data(
                    admission_event_payload(ticket, latest["admission_token"])
                )
                return

            last_beat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is not None and message.get("type") == "message":
                    data = message.get("data")
                    if isinstance(data, bytes):
                        data = data.decode()
                    if not data:
                        continue
                    yield _sse_data(data if isinstance(data, str) else json.dumps(data))
                    return
                if time.monotonic() - last_beat >= SSE_HEARTBEAT_SECONDS:
                    # SSE comment: browsers and proxies treat this as activity
                    # so an idle wait of minutes does not get cut as a timeout.
                    yield ": heartbeat\n\n"
                    last_beat = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if is_transient_backend_error(exc):
                return
            raise
        finally:
            sse_connection_closed()
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(admission_channel(ticket))
                except Exception:
                    pass
                try:
                    await pubsub.aclose()
                except Exception:
                    pass

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
