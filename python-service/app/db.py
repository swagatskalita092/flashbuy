"""Database engine, sessions, and additive schema upgrades.

`create_all` does not alter existing tables, so after Phase 1 we add new
order columns with `IF NOT EXISTS` SQL. That lets `docker compose up` keep
the named Postgres volume instead of requiring a wipe for every schema tweak.
"""

import os
from collections.abc import AsyncGenerator

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://flashbuy:flashbuy@localhost:5432/flashbuy",
)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    # SQLAlchemy default is pool_size=5, max_overflow=10 (15 checkouts at once).
    # Join still hits Postgres to verify the product exists, and checkout holds
    # a row lock until commit. 500 users spawn at 25/s with up to 20 admissions
    # per second, so 15 connections queue or time out and FastAPI turns that
    # into a 500. 30+50=80 stays under Postgres max_connections (we set 200 in
    # compose). pool_timeout=10 fails fast so we can return 503 instead of hanging.
    pool_size=int(os.getenv("DB_POOL_SIZE", "30")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "50")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "10")),
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    """Shared declarative base so metadata.create_all sees every model."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield one session per request so commits/rollbacks stay request-scoped.

    The session is closed (and any *uncommitted* work rolled back) when the
    request finishes. Checkout therefore must `commit()` before it returns 201.
    Closing the session does not undo a commit that already landed.
    """
    async with SessionLocal() as session:
        yield session


async def order_persisted(order_id, async_engine: AsyncEngine | None = None) -> bool:
    """True if this order id is visible on a new connection (committed, not session-only).

    We do *not* re-read stock and require it to equal this request's expected
    remaining count. Concurrent checkouts will keep decrementing after we
    commit, so stock can already be lower. The order row is the durable proof
    this 201 corresponds to a committed write.
    """
    from app.models import Order

    eng = async_engine if async_engine is not None else engine
    async with eng.connect() as conn:
        result = await conn.execute(select(Order.id).where(Order.id == order_id))
        return result.scalar_one_or_none() is not None


async def apply_schema(engine_to_use: AsyncEngine) -> None:
    """Create tables if needed, then add Phase 2 columns/indexes on old volumes."""
    from app.models import Order, Product  # noqa: F401 — register models on Base.metadata

    async with engine_to_use.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # asyncpg allows one statement per prepared execute.
        await conn.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255)"
            )
        )
        await conn.execute(
            text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
        )
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_idempotency_key
                    ON orders (idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                """
            )
        )
