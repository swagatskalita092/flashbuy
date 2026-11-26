# Phase 6 chaos-injection results (durable event delivery)

Real output from the 5 scenarios planned in `ENGINEERING_JOURNAL.md` Phase 6,
run against the actual `rabbitmq`, `postgres`, `postgres-fulfillment`,
`outbox-publisher`, and `fulfillment-consumer` containers from
`docker-compose.yml` - not simulated. Each crash is injected by an
environment-variable hook that exists only for this purpose
(`CHAOS_CRASH_AFTER_PUBLISH_CONFIRM`, `CHAOS_CRASH_AFTER_COMMIT_BEFORE_ACK`),
never set outside these runs.

Four real bugs were found and fixed during this verification effort, not
before it - each is reported below in the scenario where it surfaced, or in
the summary at the end, the same way Bug #1/#2 and Finding #3/#4 are
reported elsewhere in this journal.

## Baseline: why there is no "dual-write loss" run to show

The original plan called for reproducing the classic dual-write bug (commit
the order, then separately fail to publish) as a before/after baseline. That
bug has no code path to reproduce here: `app/routes/orders.py` only ever
writes an `OutboxEvent` row inside the exact same `db.add(...)` /
`await db.commit()` as the status change itself - there is no second,
separate operation that could fail independently. This is a structural
guarantee (one transaction, one commit), not a race that happens not to
fire, so there is nothing to trigger. The property is still real and still
worth stating plainly: it is proven by `tests/test_outbox.py`, which asserts
that `/checkout` alone (reserve, no confirm) never produces an outbox row,
and that `/confirm` always produces exactly one.

## Scenario 1: publisher crashes after a broker-confirmed publish, before marking the row published

Expectation: the row stays unpublished and gets redelivered - a real,
accepted duplicate.

```
$ docker run ... -e CHAOS_CRASH_AFTER_PUBLISH_CONFIRM=1 ... python -m app.outbox_publisher
...
publisher connected to RabbitMQ, entering poll loop
publisher CHAOS: exiting immediately after broker-confirmed publish for
  3a811967-d500-4dd3-a444-33835b5f8533, before marking it published -
  this row WILL be redelivered on restart
```

```sql
-- outbox row, immediately after the crash:
SELECT id, order_id, published_at FROM outbox_events WHERE order_id = '...';
 3a811967-... | 0503132f-... | (null)
```

The first (legitimate) delivery is consumed normally:
```
fulfillment job created for order 0503132f-a473-4cbf-8818-dba3602458f5
```

Running the same crash-publisher a second time re-reads the still-unpublished
row and republishes it - a genuine duplicate delivery of the same event_id:
```
publisher CHAOS: exiting immediately after broker-confirmed publish for
  3a811967-d500-4dd3-a444-33835b5f8533, before marking it published - ...
```

The consumer's idempotent inbox catches it:
```
fulfillment duplicate delivery of 3a811967-d500-4dd3-a444-33835b5f8533,
  already processed, no new job created
```

```
$ curl .../jobs/by-order/0503132f-a473-4cbf-8818-dba3602458f5
{"order_id":"0503132f-...","fulfillment_job_count":1}
```

One fulfillment job, despite two broker deliveries of the same event.

## Scenario 2: consumer crashes after its local commit, before acking the broker

Expectation: RabbitMQ redelivers the still-unacked message; the redelivery
must not create a second job.

```
$ docker run ... -e CHAOS_CRASH_AFTER_COMMIT_BEFORE_ACK=1 ... python -m app.consumer
...
fulfillment CHAOS: exiting immediately after committing
  78b879b5-4c56-48d1-82ba-989bc89acb98 locally, before acking the broker -
  this message WILL be redelivered on restart
```

```
$ curl .../jobs/by-order/f743b45a-7232-4325-b415-8aa8b6e0cb1a
{"order_id":"f743b45a-...","fulfillment_job_count":1}   # the commit landed
```

Restarting the real consumer (no crash hook) picks up the redelivered,
unacked message:
```
fulfillment duplicate delivery of 78b879b5-4c56-48d1-82ba-989bc89acb98,
  already processed, no new job created
fulfillment acked duplicate delivery of 78b879b5-...,
  no fulfillment job created
```

```
$ curl .../jobs/by-order/f743b45a-7232-4325-b415-8aa8b6e0cb1a
{"order_id":"f743b45a-...","fulfillment_job_count":1}   # unchanged
```

**Bug found here, fixed before this result:** the original code always
logged `"fulfillment job created for order %s"` right before acking,
even on the duplicate-delivery path where `IntegrityError` was caught and no
job was created. The ack itself was correct (a duplicate must still be
acked, or it would loop forever); the log line was simply false on that
path. Fixed by tracking `was_duplicate` and logging accordingly -
`app/consumer.py`. Caught by actually reading this scenario's output, not by
inspection.

## Scenario 3: sustained broker outage while checkouts and confirmations continue

Expectation: confirmation stays a database-only operation (never blocked or
slowed by the broker), and every event queued during the outage is
delivered once the broker comes back - none lost.

```
$ docker compose stop rabbitmq
# three checkouts + confirms against app1, broker fully down:
{"id":"423575d5-...","status":"confirmed",...}
{"id":"28d883ae-...","status":"confirmed",...}
{"id":"49ca7251-...","status":"confirmed",...}
```

All three confirmed normally - `/orders/{id}/confirm` never touches
RabbitMQ directly (see the docstring in `app/routes/orders.py`).

```sql
SELECT order_id, published_at FROM outbox_events WHERE order_id IN (...);
 423575d5-... | (null)
 28d883ae-... | (null)
 49ca7251-... | (null)
```

Publisher logs during the outage:
```
publisher connection_lost: StreamLostError: (Connection reset by peer)
publisher RabbitMQ unreachable (...), retrying in 2.0s
```

**Bug found here, fixed before the rest of this scenario's result:** on
restart, RabbitMQ briefly had a new container address and the publisher's
DNS lookup for the `rabbitmq` hostname failed with `socket.gaierror` -
which is not a subclass of `pika.exceptions.AMQPConnectionError`, so it
escaped the `except` clause entirely and crashed the whole process instead
of retrying. `docker ps` confirmed it: `Exited (1)`. Fixed by also catching
`OSError` in both `app/outbox_publisher.py` and
`fulfillment-service/app/consumer.py`'s retry loops, plus adding
`restart: on-failure` to both services (and `fulfillment-api`) in
`docker-compose.yml` as defense in depth. After the fix:

```
$ docker compose up -d rabbitmq
# (publisher restarted with the fix applied)
publisher connected to RabbitMQ, entering poll loop
publisher published and marked 3 event(s)
```

```
$ curl .../jobs/by-order/423575d5-...
{"order_id":"423575d5-...","fulfillment_job_count":1}
# (same for the other two orders)
```

All three events survived the outage and were delivered exactly once each.

## Scenario 4: a poison message must dead-letter, not block the queue

Expectation: a malformed message is nacked without requeue, lands in the
dead-letter queue, and a valid message published right after it is
unaffected.

```python
# deliberately not even valid JSON:
channel.basic_publish(exchange="flashbuy.events", routing_key="order.confirmed",
                       body=b"{not json at all", ...)
```

```
$ docker exec rabbitmq-1 rabbitmqctl list_queues name messages | grep fulfillment
fulfillment.order_confirmed       0
fulfillment.order_confirmed.dlq   1
```

A real checkout + confirm immediately after:
```
$ curl .../jobs/by-order/53149b28-2f4c-4506-bff4-ee46b45563c6
{"order_id":"53149b28-...","fulfillment_job_count":1}
```

```
$ rabbitmqctl list_queues name messages | grep fulfillment
fulfillment.order_confirmed       0
fulfillment.order_confirmed.dlq   1   # unchanged - the valid message was not blocked
```

## Bugs this verification pass actually found (summary)

Consistent with how Bug #1/#2 and Finding #3/#4 are reported elsewhere in
this journal: these were found by running the thing, not by reading the
code twice.

1. `_handle_message`'s signature took 3 positional arguments
   (`channel, method, body`); pika's `basic_consume` callback always passes
   4 (`channel, method, properties, body`). Every single message raised
   `TypeError` and crashed the consumer on the very first real message.
2. `GET /jobs/by-order/{order_id}` was typed `-> dict[str, int]` while
   returning a UUID string under the `order_id` key, so FastAPI's response
   validation rejected every response with a 500.
3. `postgres-fulfillment` and the pre-existing `postgres-test` profile both
   claimed host port 5433, so bringing up the Phase 6 services first and the
   test profile second failed with "port is already allocated." Fixed by
   moving `postgres-fulfillment` to 5434.
4. The logging bug and the uncaught-`OSError` bug described in Scenarios 2
   and 3 above.

## What this does and does not prove

At-least-once delivery, not exactly-once - stated here as plainly as
`ENGINEERING_JOURNAL.md` Phase 6 originally required. Nothing above proves
Kafka-style ordering guarantees or exactly-once semantics; it proves that
*this* consumer's idempotent inbox makes at-least-once delivery survivable,
and that the publisher's never-mark-published-without-a-confirm rule makes
a broker outage a delay, not a loss.
