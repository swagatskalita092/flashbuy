"""Database engine, sessions, and additive schema upgrades.

`create_all` does not alter existing tables, so after Phase 1 we add new
order columns with `IF NOT EXISTS` SQL. That lets `docker compose up` keep
the named Postgres volume instead of requiring a wipe for every schema tweak.
"""

import os
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://flashbuy:flashbuy@localhost:5432/flashbuy",
)

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    """Shared declarative base so metadata.create_all sees every model."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield one session per request so commits/rollbacks stay request-scoped."""
    async with SessionLocal() as session:
        yield session


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
