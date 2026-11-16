"""Tiny status API for the Fulfillment Service - not the consumer process itself.

The actual work (consuming events, writing jobs) happens in app/consumer.py,
run as its own container. This process exists only so chaos-test scripts and
a curious reviewer can ask "how many fulfillment jobs actually exist" without
opening a psql shell - e.g. to prove a duplicate delivery produced zero extra
jobs, from the outside, over HTTP.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import func, select

from app.db import SessionLocal, apply_schema
from app.models import FulfillmentJob, InboxEvent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    apply_schema()
    yield


app = FastAPI(title="FlashBuy Fulfillment Service", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stats")
def stats() -> dict[str, int]:
    """Counts used by the Phase 6 chaos tests to prove no duplicate jobs exist."""
    with SessionLocal() as session:
        jobs = session.execute(select(func.count()).select_from(FulfillmentJob)).scalar_one()
        inbox = session.execute(select(func.count()).select_from(InboxEvent)).scalar_one()
    return {"fulfillment_jobs": jobs, "inbox_events": inbox}


@app.get("/jobs/by-order/{order_id}")
def jobs_for_order(order_id: str) -> dict[str, str | int]:
    """How many fulfillment jobs exist for one order - should never exceed 1."""
    with SessionLocal() as session:
        count = session.execute(
            select(func.count())
            .select_from(FulfillmentJob)
            .where(FulfillmentJob.order_id == order_id)
        ).scalar_one()
    return {"order_id": order_id, "fulfillment_job_count": count}
