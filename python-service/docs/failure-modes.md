# Failure modes (verified, and two found but not yet fixed)

Dated **2026-09-05**, extended **2026-11-26**. Cases 1 through 4 are the
three cases Phase 5C asked for plus the frozen-leader case outside feedback
surfaced; each was triggered in pytest (or Locust, where noted), not only
reasoned about. Cases 5 and 6 are different in kind: they were found by
reading the code against two known distributed-systems failure classes
(unbounded outbox/inbox growth, and the lack of a claiming mechanism for a
scaled-out poller), not by a test or a chaos drill, and neither is fixed
here. They are recorded for the same reason the others are: so a real gap
is written down honestly instead of discovered the hard way later.

## 1. Reservation-expiry job stops

Checkout decrements stock immediately and leaves the order `reserved` until confirm or `expires_at` (default 300s). A background loop in each app process calls `expire_reservations` every `EXPIRY_SWEEP_INTERVAL_SECONDS` (default 10s).

**If that loop is not running** (killed process, or the function simply never called): clock-expired rows stay `reserved` and stock stays deducted. The unit is **stuck until a sweeper runs again**, not forever after restart.

Verified in `test_stock_stays_held_if_expiry_sweep_does_not_run`: checkout stock 1 → 0, backdate `expires_at`, **do not** sweep; product stock still 0 and status still `reserved`. Then `expire_reservations` returns 1 and stock is 1. The existing `test_expired_reservation_releases_stock` is the restart/sweep-success path.

Compose runs **three** sweepers (one per replica). That is redundant, not a lock. `SKIP LOCKED` keeps them from fighting. Stopping **one** replica does not stop expiry; stopping **all** app processes does, until one comes back.

## 2. Client timeout after a committed checkout, then retry

The server commits the order (and stock decrement) before it returns HTTP 201. A client that times out and retries **must** send the same `idempotency_key`. Unique constraint on that key plus a lookup of the existing row returns the original order, HTTP 201, **one** unit charged.

Verified in `test_duplicate_idempotency_key_does_not_create_second_order`: two POSTs, same body, same `id` in both 201s, stock down by 1 not 2. There is no extra delay injected; the second call **is** the retry after a successful commit, which is the timeout-then-retry sequence without a real network stall.

Locust uses a **new** UUID per checkout attempt, so a 503 **before** commit followed by a later checkout with a new key is a different buyer-attempt. Chaos Postgres run: 70 checkout 503s, `psql` still **500** orders / **500** keys (no double charge on that run). A 503 **after** commit with a new key on retry would be a real double-charge risk; Locust stock-exhaust does not retry checkout on 503.

## 3. SSE PUBLISH with no subscriber

Redis pub/sub does not replay. If the buyer is admitted while disconnected, waiting only for a future PUBLISH would hang until heartbeat timeout.

`GET /waiting-room/stream/{ticket_id}` calls `get_ticket_status` **before** subscribe and **again** after subscribe, then waits. An already-granted token is written as an `admission` event immediately.

Verified in `test_stream_emits_immediately_when_already_admitted`: admit first (PUBLISH with nobody listening), then open the stream; the body contains `event: admission` and the same token as `GET /waiting-room/status`. `test_stream_receives_admission_via_pubsub` is the live-subscriber path.

The 500-user `stock_exhaust_sse` Locust run through Caddy (Phase 5A) had stream p50 **7 ms** and 500/500 checkouts: many streams opened after the leader had already granted, on a **different replica** than join. That is this reconnect check under load, not a hang.

## 4. Frozen-then-resumed admission leader (fencing token)

Found by outside feedback (Edilec on LinkedIn), not by Phase 5B. Those drills only `docker kill` / `docker stop`. They never freeze a process longer than the 5s lease and then let it continue (`kill -STOP`, GC pause, frozen VM).

**What was wrong:** `_admission_loop` did `hold_admission_leadership` then, in a later Redis round trip, `admit_waiting_buyers`. A freeze between those two calls lets the lease expire and another replica take over. On resume the first process still ran admit because its check had already returned true. Two writers in one window.

A lease cannot close that. Check-then-act across two network calls is not atomic.

**Fix:** a fencing epoch on `flashbuy:admission:leader_epoch`. The counter increments only on a fresh lock acquire, not on renew. `admit_waiting_buyers` takes that epoch and re-reads it inside the same Lua script that pops the queue and writes tokens. A stale epoch returns **-1** (empty queue still returns **0**). The admission loop logs `admission rejected: stale epoch ...` on -1.

**What was actually proven:** `test_stale_fencing_epoch_is_rejected_after_a_simulated_freeze` is the direct stale-write proof: A takes an epoch, the lease is deleted, B admits on a new epoch, A retries with the old epoch and must get **-1**. That test failed (`assert 5 == 0`, later `-1`) when the Lua check was removed, then passed with it restored.

A live Locust 500-user `docker pause` of `python-service-app3-1` confirmed **no split-brain in leadership state**: `/health` moved from app3 / epoch 1 to app2 / epoch 2, and after resume app3 reported `admission_leader: false`. Orders: stock 0, **500** rows, **500** distinct idempotency keys. It did **not** prove a stale admit was refused on app3: uvicorn never logged the loop, and returning 0 used to look the same as an empty queue. The -1 return plus the log line are what make the next pause drill able to show a reject in `docker logs`.

**Separate operational gap from the same drill, not fixed here:** Caddy has no short upstream timeout. Requests that landed on the frozen replica sat until unpause (checkout/status p99 ~90s). Documented, not patched in this change. Script: `scripts/chaos_pause_leader.sh`.

## 5. Outbox and inbox tables grow without bound

The transactional outbox (`outbox_events`) and the fulfillment service's idempotent inbox (`inbox_events`) are both append-only: a row is written once and never deleted. There is no retention job, no archival step, and no `DELETE` anywhere in `app/outbox_publisher.py`, `app/models.py`, or `fulfillment-service/app/consumer.py` - confirmed by grepping both services for `DELETE FROM`, `delete(`, `retention`, and `cleanup`, which returns nothing.

At the throughput this project tests at, this is invisible. At real production volume it would not be: both tables grow forever, which eventually costs index size, vacuum time, and backup size on tables the application touches every poll cycle.

**Fixed for the outbox side.** `app/outbox_retention.py` deletes published
`outbox_events` rows older than `OUTBOX_RETENTION_DAYS` (default 7), run as
its own `outbox-retention` container on a loop, the same shape as
`outbox_publisher.py`. The safety invariant - never delete a row with
`published_at IS NULL`, regardless of age - is written into the query itself
(it filters on `published_at IS NOT NULL` before it ever filters on age),
not just asserted in prose, and `tests/test_outbox_retention.py` checks that
invariant directly with a row backdated 365 days and a cutoff permissive
enough to catch it by age alone, plus the ordinary delete/keep/batch-limit
cases. **This was written without the project's own Postgres/RabbitMQ
containers available to run it against** - the tests have not actually been
executed yet, only compiled for syntax, so this is a candidate fix, not a
proven one, until `./scripts/run_isolated_tests.sh` has actually been run
and the new tests pass. `inbox_events` on the fulfillment-service side has
the identical gap and is not yet fixed - same shape, separate work.

## 6. The outbox publisher is safe today only because exactly one instance runs

`_fetch_unpublished_batch` in `app/outbox_publisher.py` selects every row with `published_at IS NULL`, ordered by `created_at`, with no row-level claim: no `SELECT ... FOR UPDATE SKIP LOCKED`, no lease, no owner column. `docker-compose.yml` runs exactly one `outbox-publisher` container, so this is safe as deployed.

If that service were ever scaled to two or more replicas for throughput, both would poll the same unclaimed rows and both would attempt to publish them - a real double-publish, not the single-publisher crash-and-retry duplicate the consumer's idempotent inbox is already built to tolerate. The inbox would still stop a duplicate *fulfillment job* from being created, so this would not corrupt data, but it would waste broker messages and complicate monitoring.

**Not fixed here.** This is a scaling ceiling in the current design, not a bug in the system as it runs today. Documenting it is the point: a future change to run more than one publisher replica should not ship without the claiming logic it would then need.
