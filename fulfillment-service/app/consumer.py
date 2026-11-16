"""Fulfillment Service consumer: the idempotent-inbox side of Phase 6.

Consumes `order.confirmed` events from RabbitMQ with manual acknowledgement:
a message is only acked *after* the local transaction (inbox row + job row,
one commit) has succeeded. If this process crashes between committing and
acking, RabbitMQ redelivers the message on reconnect - and the unique
constraint on `inbox_events.event_id` makes that redelivery a safe no-op
instead of a duplicate fulfillment job. That is the entire point of Phase 6:
at-least-once delivery becomes survivable because the *consumer*, not the
broker, is where exactly-once side effects actually get enforced.

A malformed ("poison") message fails validation before any DB write is
attempted and is nacked without requeue, so RabbitMQ routes it to the dead-
letter exchange/queue configured in docker-compose.yml instead of blocking
every valid message queued behind it.
"""

import json
import logging
import os
import time

import pika
import pika.exceptions
from sqlalchemy.exc import IntegrityError

from app.db import apply_schema, session_scope
from app.models import FulfillmentJob, InboxEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s fulfillment %(message)s")
log = logging.getLogger("fulfillment_consumer")

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://flashbuy:flashbuy@localhost:5672/")
EXCHANGE_NAME = "flashbuy.events"
QUEUE_NAME = "fulfillment.order_confirmed"
DLX_NAME = "flashbuy.events.dlx"
DLQ_NAME = "fulfillment.order_confirmed.dlq"
ROUTING_KEY = "order.confirmed"

# Chaos-injection-only: exit hard immediately after the local DB commit for
# the *first* message of the run, before acking the broker. Proves a
# redelivered message after this crash does not create a second job.
_CRASH_AFTER_FIRST_COMMIT = os.getenv("CHAOS_CRASH_AFTER_COMMIT_BEFORE_ACK") == "1"
_crashed_once = False


def _declare_topology(channel: pika.adapters.blocking_connection.BlockingChannel) -> None:
    channel.exchange_declare(exchange=EXCHANGE_NAME, exchange_type="topic", durable=True)
    channel.exchange_declare(exchange=DLX_NAME, exchange_type="fanout", durable=True)

    channel.queue_declare(
        queue=QUEUE_NAME,
        durable=True,
        arguments={"x-dead-letter-exchange": DLX_NAME},
    )
    channel.queue_bind(queue=QUEUE_NAME, exchange=EXCHANGE_NAME, routing_key=ROUTING_KEY)

    channel.queue_declare(queue=DLQ_NAME, durable=True)
    channel.queue_bind(queue=DLQ_NAME, exchange=DLX_NAME)


def _parse_and_validate(body: bytes) -> dict:
    """Raise ValueError on anything that is not a well-formed order.confirmed event."""
    data = json.loads(body)  # raises json.JSONDecodeError on garbage bytes
    for field in ("event_id", "event_type", "order_id", "payload"):
        if field not in data:
            raise ValueError(f"missing required field: {field}")
    if data["event_type"] != "order.confirmed":
        raise ValueError(f"unexpected event_type: {data['event_type']}")
    return data


def _handle_message(
    channel: pika.adapters.blocking_connection.BlockingChannel,
    method: pika.spec.Basic.Deliver,
    _properties: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    global _crashed_once
    try:
        event = _parse_and_validate(body)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("poison message, routing to DLQ: %s", exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    was_duplicate = False
    try:
        with session_scope() as session:
            session.add(
                InboxEvent(event_id=event["event_id"], event_type=event["event_type"])
            )
            session.add(
                FulfillmentJob(order_id=event["order_id"], payload=event["payload"])
            )
            session.commit()
    except IntegrityError:
        # uq_inbox_events_event_id fired: we've already processed this
        # event_id before (a redelivery). Not an error - this is the
        # idempotency guarantee working as designed. No new job is created.
        was_duplicate = True
        log.info("duplicate delivery of %s, already processed, no new job created", event["event_id"])
    except Exception:
        log.exception("processing failed for %s, nacking for redelivery", event.get("event_id"))
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return

    if _CRASH_AFTER_FIRST_COMMIT and not _crashed_once and not was_duplicate:
        _crashed_once = True
        log.warning(
            "CHAOS: exiting immediately after committing %s locally, before "
            "acking the broker - this message WILL be redelivered on restart",
            event["event_id"],
        )
        os._exit(1)

    channel.basic_ack(delivery_tag=method.delivery_tag)
    if was_duplicate:
        log.info("acked duplicate delivery of %s, no fulfillment job created", event["event_id"])
    else:
        log.info("fulfillment job created for order %s", event["order_id"])


def main() -> None:
    apply_schema()
    log.info("starting fulfillment consumer, broker=%s", RABBITMQ_URL)
    while True:
        try:
            connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
            channel = connection.channel()
            _declare_topology(channel)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=_handle_message)
            log.info("consuming from %s", QUEUE_NAME)
            channel.start_consuming()
        except (pika.exceptions.AMQPConnectionError, OSError) as exc:
            # See the matching comment in app/outbox_publisher.py: OSError
            # catches a DNS lookup failure for the broker's hostname, which
            # AMQPConnectionError alone does not.
            log.warning("RabbitMQ unreachable (%s), retrying in 2s", exc)
            time.sleep(2)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
