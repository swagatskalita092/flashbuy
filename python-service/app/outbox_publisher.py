"""Phase 6 outbox publisher: the only process that talks to RabbitMQ for events.

Runs as its own container (see docker-compose.yml: `outbox-publisher`), polling
`outbox_events` for rows with `published_at IS NULL` and delivering each one to
RabbitMQ using publisher confirms - the broker's "I have accepted responsibility
for this message" guarantee. A row is marked published only *after* that
confirm comes back, never before and never optimistically.

Why this process exists at all, instead of publishing inline in the checkout/
confirm request: the API request that writes the outbox row must never block
on - or fail because of - the message broker being slow or down. Confirming an
order is a database-only operation (see app/routes/orders.py). Delivering the
resulting event is this process's job, on its own retry loop, indefinitely.

Failure modes this is deliberately built to survive, each proven in
docs/phase6_chaos_results.md:
  - This process crashes after a DB commit but before ever publishing: the
    row is still `published_at IS NULL` on restart, so it is simply retried.
    Nothing is lost - there is no separate "did I publish yet" state to lose,
    because the only durable record of intent is the outbox row itself.
  - This process crashes after RabbitMQ confirms receipt but before the local
    UPDATE commits: on restart the row still looks unpublished and gets
    published again. This is a real, deliberately-accepted duplicate
    delivery - the consumer side (fulfillment-service) is the thing that
    must be idempotent, not this process. At-least-once, not exactly-once.
  - RabbitMQ is unreachable: publishing raises, this loop logs and retries
    on the next tick. No outbox row is ever marked published without a
    confirm, so a sustained outage just delays delivery, it cannot drop it.
"""

import asyncio
import json
import logging
import os
import time
import uuid

import pika
import pika.exceptions
from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import OutboxEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s publisher %(message)s")
log = logging.getLogger("outbox_publisher")

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://flashbuy:flashbuy@localhost:5672/")
EXCHANGE_NAME = "flashbuy.events"
POLL_INTERVAL_SECONDS = float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "1"))
BATCH_SIZE = int(os.getenv("OUTBOX_BATCH_SIZE", "20"))

# Chaos-injection-only: when set, this process exits hard immediately after the
# *first* message of the run is broker-confirmed but before the local row is
# marked published - deliberately reproducing the "confirmed but not yet
# marked" crash window. Never set outside docs/phase6_chaos_results.md runs.
_CRASH_AFTER_FIRST_CONFIRM = os.getenv("CHAOS_CRASH_AFTER_PUBLISH_CONFIRM") == "1"


def _connect() -> pika.BlockingConnection:
    params = pika.URLParameters(RABBITMQ_URL)
    params.heartbeat = 30
    return pika.BlockingConnection(params)


def _declare_topology(channel: pika.adapters.blocking_connection.BlockingChannel) -> None:
    channel.exchange_declare(exchange=EXCHANGE_NAME, exchange_type="topic", durable=True)


async def _fetch_unpublished_batch() -> list[OutboxEvent]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.created_at)
            .limit(BATCH_SIZE)
        )
        return list(result.scalars().all())


async def _mark_published(event_id: uuid.UUID) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("UPDATE outbox_events SET published_at = now() WHERE id = :id"),
            {"id": str(event_id)},
        )
        await session.commit()


def _run_once(channel: pika.adapters.blocking_connection.BlockingChannel, events: list[OutboxEvent]) -> list[uuid.UUID]:
    """Publish each event with a confirm; return the ids that are now confirmed."""
    confirmed: list[uuid.UUID] = []
    first = True
    for event in events:
        body = json.dumps(
            {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "order_id": str(event.order_id),
                "payload": event.payload,
            }
        ).encode("utf-8")
        try:
            # confirm_delivery() (enabled once on the channel below) makes
            # basic_publish raise UnroutableError/NackError on a broker nack
            # instead of silently returning - that raise is what "publisher
            # confirms" means in practice with pika's blocking adapter.
            channel.basic_publish(
                exchange=EXCHANGE_NAME,
                routing_key=event.event_type,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,  # persistent
                    message_id=str(event.id),
                    content_type="application/json",
                ),
                mandatory=True,
            )
        except (pika.exceptions.UnroutableError, pika.exceptions.NackError) as exc:
            log.warning("publish not confirmed for %s, will retry next poll: %s", event.id, exc)
            break  # stop this batch; unconfirmed rows stay unpublished, retried next tick

        if _CRASH_AFTER_FIRST_CONFIRM and first:
            log.warning(
                "CHAOS: exiting immediately after broker-confirmed publish for %s, "
                "before marking it published - this row WILL be redelivered on restart",
                event.id,
            )
            os._exit(1)
        first = False
        confirmed.append(event.id)
    return confirmed


async def _mark_all_published(ids: list[uuid.UUID]) -> None:
    for event_id in ids:
        await _mark_published(event_id)


async def _poll_loop() -> None:
    log.info("starting outbox publisher, broker=%s", RABBITMQ_URL)
    while True:
        try:
            connection = _connect()
            channel = connection.channel()
            channel.confirm_delivery()
            _declare_topology(channel)
            log.info("connected to RabbitMQ, entering poll loop")
            while True:
                events = await _fetch_unpublished_batch()
                if events:
                    confirmed_ids = _run_once(channel, events)
                    if confirmed_ids:
                        await _mark_all_published(confirmed_ids)
                        log.info("published and marked %d event(s)", len(confirmed_ids))
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except (pika.exceptions.AMQPConnectionError, OSError) as exc:
            # OSError catches what AMQPConnectionError does not: a DNS lookup
            # failure (socket.gaierror) for the broker's hostname, seen in
            # practice during the Phase 6 chaos run right after restarting
            # RabbitMQ, before Docker's embedded DNS had the new container's
            # address. Without this, that one exception type escaped the
            # retry loop entirely and crashed the whole process instead of
            # just waiting for the next tick - exactly the kind of outage
            # this loop exists to survive.
            log.warning("RabbitMQ unreachable (%s), retrying in %ss", exc, POLL_INTERVAL_SECONDS * 2)
            await asyncio.sleep(POLL_INTERVAL_SECONDS * 2)


def main() -> None:
    try:
        asyncio.run(_poll_loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
