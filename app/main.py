"""FlashBuy API process: schema, seed, reservation sweeper, HTTP routes."""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import SessionLocal, apply_schema, engine
from app.reservations import expire_reservations
from app.routes.checkout import router as checkout_router
from app.routes.orders import router as orders_router
from app.routes.products import router as products_router
from app.seed import seed_default_product

# How often we look for abandoned reservations. Tests call expire_reservations
# directly so they do not wait on this interval.
EXPIRY_SWEEP_INTERVAL_SECONDS = float(os.getenv("EXPIRY_SWEEP_INTERVAL_SECONDS", "10"))


async def _reservation_sweep_loop() -> None:
    """Wake periodically and recycle expired holds.

    This is a simple asyncio loop instead of Redis/Celery because the demo
    has one app process. The important part is *that* expiry runs, not the
    scheduler brand. Errors are swallowed per tick so a blip does not kill
    the API process.
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create/upgrade schema, seed demo stock, start the expiry sweeper."""
    await apply_schema(engine)
    async with SessionLocal() as session:
        await seed_default_product(session)
    sweeper = asyncio.create_task(_reservation_sweep_loop())
    yield
    sweeper.cancel()
    try:
        await sweeper
    except asyncio.CancelledError:
        pass
    await engine.dispose()


app = FastAPI(
    title="FlashBuy",
    description="Flash-sale checkout with row locks, idempotency, and reservation expiry.",
    lifespan=lifespan,
)

app.include_router(products_router)
app.include_router(checkout_router)
app.include_router(orders_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe used by local scripts before firing load."""
    return {"status": "ok"}
