# Failure modes (verified)

Dated **2026-09-05**. These are the three cases Phase 5C asked for. Each was triggered in pytest (or Locust, where noted), not only reasoned about.

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
