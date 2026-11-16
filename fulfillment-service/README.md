# fulfillment-service

A second, independent service that exists only to prove one property: a
committed order confirmation in `python-service` must eventually cause
exactly one logical downstream effect here, despite RabbitMQ only
guaranteeing at-least-once delivery.

This is Phase 6 of the FlashBuy engineering journal
(`python-service/docs/ENGINEERING_JOURNAL.md`). Read that first - this
README only covers what lives in this directory.

- `app/consumer.py` - the actual worker. Consumes `order.confirmed` events
  with manual acknowledgement, and only acks after a local transaction
  (inbox row + job row, one commit) succeeds. Run as its own container.
- `app/main.py` - a tiny status API (`/stats`, `/jobs/by-order/{id}`) used by
  the chaos-test scripts to prove, from the outside, that duplicate
  deliveries never produced duplicate jobs.
- `app/models.py` - `InboxEvent` (the idempotency ledger) and
  `FulfillmentJob` (the actual side effect), in this service's own
  Postgres database - never the order service's.

This service has no knowledge of `python-service`'s database, schema, or
internal state. The only thing connecting them is the RabbitMQ event.
