# FlashBuy

Flash-sale checkout backend.

- Phase 1: naive checkout that oversells under concurrency.
- Phase 2: row locks, idempotency keys, reservation expiry.
- Phase 4: Prometheus metrics, Grafana, Locust journey test, recorded numbers.

## Run locally

```bash
docker-compose up --build
```

- API: http://localhost:8000
- Metrics: http://localhost:8000/metrics
- Grafana: http://localhost:3000 (user `admin` / password `admin`) — dashboard **FlashBuy flash sale** under folder FlashBuy
- Prometheus: http://localhost:9090
- Locust UI: http://localhost:8089
- Docs: http://localhost:8000/docs
- Postgres: localhost:5432 (`flashbuy` / `flashbuy` / database `flashbuy`)
- Redis: localhost:6379

Seed product on startup:

| Field | Value |
| --- | --- |
| id | `00000000-0000-4000-8000-000000000001` |
| name | Flash Deal Widget |
| stock | 500 |
| price_cents | 1999 |

## Waiting room flow (manual)

Checkout from a real client needs a short-lived **admission token**. Join the
line, poll until admitted, then checkout. The background loop admits 20 waiters
per product per second by default (`ADMISSION_BATCH_SIZE` / `ADMISSION_TICK_SECONDS`).

```bash
# 1. Join the line
curl -X POST http://localhost:8000/waiting-room/join \
  -H "Content-Type: application/json" \
  -d '{"product_id":"00000000-0000-4000-8000-000000000001","buyer_id":"buyer-1"}'
# -> ticket_id, position

# 2. Poll until admitted is true (and you receive admission_token)
curl http://localhost:8000/waiting-room/status/TICKET_ID

# 3. Checkout with that token (2 minute TTL)
curl -X POST http://localhost:8000/checkout \
  -H "Content-Type: application/json" \
  -d '{"product_id":"00000000-0000-4000-8000-000000000001","buyer_id":"buyer-1","idempotency_key":"attempt-1","admission_token":"TOKEN"}'
```

`POST /waiting-room/join` is rate-limited (token bucket): 5 joins/minute per
`buyer_id`, 20 per IP. Excess returns **429**.

Too high an admission rate slams Postgres again; too low leaves stock idle
while the line crawls. Tune the batch size against checkout latency.

Inventory tests and `scripts/prove_race_condition.py` may send
`X-FlashBuy-Test-Bypass: 1` to skip the room. That header is **internal
testing only**, not a customer feature.

## Phase 2 behaviour (still applies)

- Checkout `SELECT ... FOR UPDATE`s the product, creates a **reserved** order
  (5 minute `expires_at`), decrements stock.
- Duplicate `idempotency_key` returns the original order.
- `POST /orders/{order_id}/confirm` moves `reserved` → `confirmed`.
- A sweeper expires abandoned holds and returns stock.

Race proof: [docs/race-condition-proof.md](docs/race-condition-proof.md).

```bash
python scripts/prove_race_condition.py
```

## Load test (Locust)

This is the whole buyer journey (join → poll status ~1s → checkout with token),
not a raw `/checkout` hammer.

Headless (what produced [docs/load_test_results.md](docs/load_test_results.md)):

```bash
docker compose run --rm locust locust -f locustfile.py --host http://app:8000 \
  --headless --users 500 --spawn-rate 25 --run-time 3m
```

Or open http://localhost:8089 after `docker compose up` and start a test there.
Each run creates a **new** product with stock 500.

**Headline from the recorded 500-user run:** 500 successful checkouts, final
stock **0**, zero oversell, checkout p50 **140ms** / p95 **780ms**. A 2000-user
attempt on this laptop saturated Locust CPU and is not used for latency
claims — details in the results doc.

## Tests

Postgres and Redis must be reachable (`docker-compose up`). Then:

```bash
pip install -r requirements.txt
pytest
```
