"""Fulfillment Service's own database - deliberately separate from FlashBuy's.

Phase 6 exists to prove that two *independent* services can agree on "did this
happen" despite unreliable message delivery. That claim is only meaningful if
this service cannot see the order service's database at all: it learns about
confirmed orders exclusively through the RabbitMQ event, never through a
shared connection, a shared schema, or a shared transaction. Hence a separate
Postgres container (see docker-compose.yml: `postgres-fulfillment`), not just
a separate schema on the same instance.
"""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://fulfillment:fulfillment@localhost:5433/fulfillment",
)

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    pass


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def apply_schema() -> None:
    from app.models import FulfillmentJob, InboxEvent  # noqa: F401

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
