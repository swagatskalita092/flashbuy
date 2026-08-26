# FlashBuy

Flash-sale checkout backend. Phase 1 shipped a **deliberately unsafe** checkout
(read stock, write order, decrement, no lock). Phase 2 proves that race, then
fixes it with row locks, idempotency keys, and reservation expiry.

## Run locally

```bash
docker-compose up --build
```

- API: http://localhost:8000
- Docs: http://localhost:8000/docs
- Postgres: localhost:5432 (`flashbuy` / `flashbuy` / database `flashbuy`)

On startup the app creates/upgrades tables and seeds one product:

| Field | Value |
| --- | --- |
| id | `00000000-0000-4000-8000-000000000001` |
| name | Flash Deal Widget |
| stock | 500 |
| price_cents | 1999 |

If you still have a Phase 1 volume and checkout errors on missing columns,
reset with `docker compose down -v` then `docker compose up --build`.

## Phase 2 behaviour

- `POST /checkout` takes `{ product_id, buyer_id, idempotency_key }`.
  It `SELECT ... FOR UPDATE` the product row, then creates a **reserved**
  order (`expires_at` = now + 5 minutes) and decrements stock. Concurrent
  buyers wait on that lock instead of both reading stale stock.
- Repeating the same `idempotency_key` returns the original order and does
  **not** take a second unit (client retries after timeouts).
- `POST /orders/{order_id}/confirm` moves `reserved` → `confirmed` (fake payment).
- A background sweeper expires stale reservations and returns the unit to stock.

## Prove overselling / the fix

```bash
python scripts/prove_race_condition.py
```

Before/after numbers: [docs/race-condition-proof.md](docs/race-condition-proof.md).

## Tests

Postgres must be reachable. Then:

```bash
pip install -r requirements.txt
pytest
```
