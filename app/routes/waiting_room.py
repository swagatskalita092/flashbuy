"""HTTP API for the virtual waiting room (join + poll). Rate limits live here, not on checkout."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Product
from app.metrics import join_latency_seconds, observe_latency, record_rejection
from app.rate_limit import allow_waiting_room_join
from app.redis_client import get_redis
from app.schemas import WaitingRoomJoinRequest, WaitingRoomStatusOut
from app.waiting_room import get_ticket_status, join_queue

router = APIRouter(prefix="/waiting-room", tags=["waiting-room"])


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
        product = await db.get(Product, payload.product_id)
        if product is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")

        redis = await get_redis()
        allowed, reason = await allow_waiting_room_join(redis, payload.buyer_id, client_ip(request))
        if not allowed:
            record_rejection("rate_limited")
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

        return await join_queue(redis, str(payload.product_id), payload.buyer_id)


@router.get("/status/{ticket_id}", response_model=WaitingRoomStatusOut)
async def waiting_room_status(ticket_id: uuid.UUID) -> dict:
    """Poll place-in-line. Clients should not hammer this; admission is server-side."""
    redis = await get_redis()
    result = await get_ticket_status(redis, str(ticket_id))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found")
    return result
