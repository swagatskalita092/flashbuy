"""FlashBuy API process: schema, seed, reservation sweeper, waiting-room admission."""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.capacity import is_transient_backend_error

from app.db import SessionLocal, apply_schema, engine
from app.redis_client import close_redis, get_redis
from app.reservations import expire_reservations
from app.routes.checkout import router as checkout_router
from app.routes.orders import router as orders_router
from app.routes.products import router as products_router
from app.routes.waiting_room import router as waiting_room_router
from app.seed import seed_default_product
from app.waiting_room import ADMISSION_TICK_SECONDS, admit_waiting_buyers

# How often we look for abandoned reservations. Tests call expire_reservations
# directly so they do not wait on this interval.
EXPIRY_SWEEP_INTERVAL_SECONDS = float(os.getenv("EXPIRY_SWEEP_INTERVAL_SECONDS", "10"))


async def _reservation_sweep_loop() -> None:
    """Wake periodically and recycle expired holds.

    Inventory expiry still belongs in Postgres: Redis does not know stock.
    Errors are swallowed per tick so a blip does not kill the API process.
    """
    while True:
        await asyncio.sleep(EXPIRY_SWEEP_INTERVAL_SECONDS)
        try:
            async with SessionLocal() as session:
                await expire_reservations(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


async def _admission_loop() -> None:
    """Drip-feed the waiting room every tick.

    Sleep-then-work (not work-then-sleep) so a slow Redis does not overlap
    ticks. Failures skip a beat instead of crashing uvicorn.
    """
    while True:
        await asyncio.sleep(ADMISSION_TICK_SECONDS)
        try:
            redis = await get_redis()
            await admit_waiting_buyers(redis)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create/upgrade schema, seed demo stock, connect Redis, start background loops."""
    await apply_schema(engine)
    async with SessionLocal() as session:
        await seed_default_product(session)
    await get_redis()
    sweeper = asyncio.create_task(_reservation_sweep_loop())
    admission = asyncio.create_task(_admission_loop())
    yield
    sweeper.cancel()
    admission.cancel()
    for task in (sweeper, admission):
        try:
            await task
        except asyncio.CancelledError:
            pass
    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="FlashBuy",
    description="Flash-sale checkout with waiting room, rate limits, row locks, and reservation expiry.",
    lifespan=lifespan,
)

app.include_router(products_router)
app.include_router(checkout_router)
app.include_router(orders_router)
app.include_router(waiting_room_router)


@app.exception_handler(Exception)
async def _transient_to_503(_request: Request, exc: Exception) -> JSONResponse:
    """Turn pool/timeout errors from Depends(get_db) into 503, not a generic 500."""
    if is_transient_backend_error(exc):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "temporarily unavailable: connection pool exhausted or backend timeout"
            },
        )
    raise exc


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape target. Operators graph these; buyers never call this URL."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe used by local scripts before firing load."""
    return {"status": "ok"}
