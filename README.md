# FlashBuy

Flash-sale checkout backend. This phase is a working but **intentionally naive** implementation: checkout reads stock, then writes an order and decrements inventory with no locking, idempotency, or queue. Under concurrent load it will oversell. That is by design and will be fixed in the next phase.

## Run locally

```bash
docker-compose up --build
```

- API: http://localhost:8000
- Docs: http://localhost:8000/docs
- Postgres: localhost:5432 (`flashbuy` / `flashbuy` / database `flashbuy`)

On startup the app creates tables and seeds one product:

| Field | Value |
| --- | --- |
| id | `00000000-0000-4000-8000-000000000001` |
| name | Flash Deal Widget |
| stock | 500 |
| price_cents | 1999 |

## Endpoints

- `POST /products` — create a product `{ name, stock, price_cents }`
- `GET /products/{id}` — current stock and details
- `POST /checkout` — `{ product_id, buyer_id }` — confirms an order if `stock > 0`, otherwise `409 out of stock`

Example:

```bash
curl -X POST http://localhost:8000/checkout \
  -H "Content-Type: application/json" \
  -d '{"product_id":"00000000-0000-4000-8000-000000000001","buyer_id":"buyer-1"}'
```

## Tests

Postgres must be reachable (for example via `docker-compose up postgres`). Then:

```bash
pip install -r requirements.txt
pytest
```

Concurrency / load tests come in a later phase.
