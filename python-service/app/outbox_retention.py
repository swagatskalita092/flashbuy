"""Phase 6 follow-up: delete old, already-published outbox rows.

This closes the gap recorded in docs/failure-modes.md #5: `outbox_events` is
append-only and was never given a retention path, so it grows forever. This
module is the fix - a separate, deliberately narrow operation from the
publisher in app/outbox_publisher.py, which only ever reads and marks rows,
never deletes them.

Safety invariant this has to hold, stated as a falsifiable sentence: a row is
only ever deleted after `published_at` is set (it was actually delivered) and
only once it is older than the retention window - never an unpublished row,
regardless of age. Deleting an unpublished row would silently drop an event
this service promised to deliver; that must never happen, no matter how old
the row is, so the query below filters on `published_at IS NOT NULL` before
it ever filters on age.

This module exposes the delete as a plain, testable function
(`delete_published_outbox_events`) and a thin `main()` that runs it in a loop
as its own container (see docker-compose.yml: `outbox-retention`), the same
split app/outbox_publisher.py uses between its logic and its own `main()`.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import OutboxEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s outbox_retention %(message)s")
log = logging.getLogger("outbox_retention")

# How long a published row is kept before it is eligible for deletion. Default
# 7 days: long enough to debug a delivery question against the row itself,
# short enough that the table does not grow unbounded in normal operation.
RETENTION_DAYS = float(os.getenv("OUTBOX_RETENTION_DAYS", "7"))
SWEEP_INTERVAL_SECONDS = float(os.getenv("OUTBOX_RETENTION_SWEEP_INTERVAL_SECONDS", "3600"))
# Cap per sweep so one run cannot take an exclusive lock over a huge range in
# a single statement; the loop simply catches up over several ticks instead.
BATCH_LIMIT = int(os.getenv("OUTBOX_RETENTION_BATCH_LIMIT", "1000"))


async def delete_published_outbox_events(
    session: AsyncSession, *, older_than: datetime, limit: int = BATCH_LIMIT
) -> int:
    """Delete published outbox rows older than `older_than`. Returns rows deleted.

    `older_than` must be timezone-aware (the column is `DateTime(timezone=True)`).
    Deliberately does not compute a cutoff from "days" here - the caller does
    that - so this function has exactly one job and is trivial to call with a
    fixed, deterministic timestamp from a test.

    Postgres has no `DELETE ... LIMIT` directly, so the row count is capped
    by deleting only ids an inner SELECT already picked, one statement per
    call; a sustained backlog is drained over several sweep ticks rather than
    one statement holding a lock over an unbounded range.
    """
    ids_subquery = (
        select(OutboxEvent.id)
        .where(OutboxEvent.published_at.is_not(None))
        .where(OutboxEvent.published_at < older_than)
        .limit(limit)
        .subquery()
    )
    result = await session.execute(
        delete(OutboxEvent).where(OutboxEvent.id.in_(select(ids_subquery.c.id)))
    )
    await session.commit()
    return result.rowcount or 0


async def _sweep_once() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    async with SessionLocal() as session:
        deleted = await delete_published_outbox_events(session, older_than=cutoff)
    if deleted:
        log.info("deleted %d published outbox row(s) older than %s", deleted, cutoff.isoformat())
    return deleted


async def main() -> None:
    log.info(
        "starting: retention=%sd, sweep every %ss, batch limit %d",
        RETENTION_DAYS, SWEEP_INTERVAL_SECONDS, BATCH_LIMIT,
    )
    while True:
        try:
            await _sweep_once()
        except Exception:
            log.exception("sweep failed, will retry next tick")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
