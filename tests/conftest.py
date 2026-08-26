"""Shared pytest fixtures: isolated schema per test module run, one client per test."""

import inspect
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://flashbuy:flashbuy@localhost:5432/flashbuy",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

from app.db import Base, apply_schema, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.redis_client import close_redis, get_redis  # noqa: E402
from app.routes.checkout import TEST_BYPASS_HEADER  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]


@pytest.fixture
async def db_engine():
    """Fresh tables so tests never see leftover reservations from a previous run."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await apply_schema(engine)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def session_factory(db_engine):
    """Direct sessions for tests that must mutate `expires_at` or call the sweeper."""
    return async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def redis_db():
    """Isolate waiting-room keys. Close the client after each test.

    pytest-asyncio gives every test a new event loop; a Redis singleton from
    the previous loop would raise 'Event loop is closed' on the next test.
    """
    redis = await get_redis()
    await redis.flushdb()
    yield redis
    await redis.flushdb()
    await close_redis()


@pytest.fixture
async def client(db_engine, session_factory, redis_db):
    """HTTP client with the internal waiting-room bypass (inventory tests only)."""

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    # Older httpx has no lifespan=; newer defaults to running it. We skip
    # lifespan so tests do not start the production sweeper on the shared engine.
    transport_kwargs = {}
    if "lifespan" in inspect.signature(ASGITransport.__init__).parameters:
        transport_kwargs["lifespan"] = "off"
    transport = ASGITransport(app=app, **transport_kwargs)
    headers = {TEST_BYPASS_HEADER: "1"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def public_client(db_engine, session_factory, redis_db):
    """Same app, but no bypass header — waiting room and 403/429 behaviour."""

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport_kwargs = {}
    if "lifespan" in inspect.signature(ASGITransport.__init__).parameters:
        transport_kwargs["lifespan"] = "off"
    transport = ASGITransport(app=app, **transport_kwargs)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def unique_key() -> str:
    """New idempotency key per request unless a test is proving replay."""
    return str(uuid.uuid4())
