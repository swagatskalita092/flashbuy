from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import Base, SessionLocal, engine
from app.routes.checkout import router as checkout_router
from app.routes.products import router as products_router
from app.seed import seed_default_product


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        await seed_default_product(session)
    yield
    await engine.dispose()


app = FastAPI(
    title="FlashBuy",
    description="Naive flash-sale checkout backend (intentionally unsafe under concurrency).",
    lifespan=lifespan,
)

app.include_router(products_router)
app.include_router(checkout_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
