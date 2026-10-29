# FlashBuy

FlashBuy is a flash-sale checkout backend. It sells a limited number of units under concurrent demand without overselling, by combining a Redis waiting room, row-level locks in PostgreSQL, idempotency keys, and reservation expiry.

Headline numbers from the committed 2026-09-16 Locust run: 500 users ramped at 25 per second for 3 minutes, stock 500, three app replicas (`app1`, `app2`, `app3`) behind Caddy. Evidence is in [evidence/baseline_rerun_2026-09-16_clean/](evidence/baseline_rerun_2026-09-16_clean/). Machine and reproduce command are in [docs/reproducibility.md](docs/reproducibility.md). Full tables and method, including earlier dates, are in [docs/load_test_results.md](docs/load_test_results.md). Verified failure modes, including the frozen-leader fencing case, are in [docs/failure-modes.md](docs/failure-modes.md). Build history is in [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md).

Peak checkout rate is bounded by the admission cap (`ADMISSION_BATCH_SIZE` default 20 per tick, `ADMISSION_TICK_SECONDS` default 1, so 20 buyers per second per product). 17.90 req/s is not a throughput limit of the service.

The committed run used Locust 2.46.4 in the container. `requirements.txt` now pins Locust 2.47.0.

| Metric | Result |
| --- | --- |
| Concurrent users tested | 500 (spawn 25/s, 3 minutes) |
| Successful checkouts | 500 of 500 (HTTP 201) |
| Final stock | 0 (zero oversold) |
| Checkout latency (p50 / p95 / p99) | 150 ms / 790 ms / 1.2 s (1200 ms) |
| Peak checkout RPS | 17.90 (admission cap 20 buyers/s) |

### What these numbers measure

Latency figures are per request, as Locust records them. The checkout p50 / p95 / p99 is the `POST /checkout` call only. Time spent in the waiting room is recorded separately as `POST /waiting-room/join` and `GET /waiting-room/status` and is not included in the checkout latency. Peak checkout RPS (17.90) is the highest per-second value in the Locust history file. The average checkout rate over the full 3 minutes was about 15.5 req/s (`Requests/s` column of `baseline_rerun_stats.csv`).

## Why this matters

Flash sales fail in a specific way. Thousands of people hit the same SKU in the same second, whether that is a concert on-sale, a Black Friday drop, or a console restock. The naive read-then-write path lets two transactions both see `stock > 0` and both succeed, so the shop sells more units than it has. Even after the write path is correct, the database still cannot absorb every concurrent checkout. Connections, lock waits, and query time pile up, and the site times out for everyone, including people who would have gotten a unit.

A waiting room that is correct but **polls** still fails at scale: 5000 clients asking "am I admitted yet?" once a second is thousands of Redis round trips that almost always say no. FlashBuy admits on a drip and **pushes** the token over SSE when that buyer's turn comes. Polling remains as a fallback for clients that cannot hold a stream.

That is a production infrastructure problem, not a toy race condition. Commercial virtual waiting rooms and anti-oversell systems (for example Queue-it) exist because e-commerce and ticketing sites need to drip buyers into checkout instead of opening a database transaction per visitor. FlashBuy is a small implementation of that same category of system. It is not a product competing with those vendors.

For the full phase-by-phase build history (every bug found, every load test run, and the reasoning behind what was and was not added), see [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md), [docs/load_test_results.md](docs/load_test_results.md), [docs/reproducibility.md](docs/reproducibility.md), and [docs/failure-modes.md](docs/failure-modes.md).

## Architecture

Buyer traffic goes through the waiting room before it can take a PostgreSQL row lock. Caddy load-balances across three FastAPI replicas. Every replica runs the admission loop each tick; only the Redis lock holder admits, so replicas cannot multiply the admission rate. Observability is a separate scrape path. It does not sit on the checkout hot path.

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 80, "rankSpacing": 100, "curve": "linear"}}}%%
flowchart TB
  buyer[Buyer]
  caddy[Caddy load balancer]

  buyer --> caddy

  subgraph replicas [FastAPI replicas]
    direction LR
    subgraph r1 [FastAPI replica 1]
      j1["POST /waiting-room/join"]
      c1["POST /checkout"]
      l1["admission loop ticks every 1s default"]
    end
    subgraph r2 [FastAPI replica 2]
      j2["POST /waiting-room/join"]
      c2["POST /checkout"]
      l2["admission loop ticks every 1s default"]
    end
    subgraph r3 [FastAPI replica 3]
      j3["POST /waiting-room/join"]
      c3["POST /checkout"]
      l3["admission loop ticks every 1s default"]
    end
  end

  caddy --> j1
  caddy --> c1
  caddy --> j2
  caddy --> c2
  caddy --> j3
  caddy --> c3

  redis[(Redis)]
  lock["Leader lock Lua: GET, SET owner EX 5s if empty, EXPIRE to renew. Epoch INCR on acquire"]
  admit["Only lock holder admits N=20 per product per tick, env-overridable. Admit Lua re-checks epoch"]
  pg[(PostgreSQL)]
  prom[Prometheus]
  graf[Grafana]

  j1 -->|"429 if rate limited, buyer_id plus IP"| redis
  j2 -->|"429 if rate limited, buyer_id plus IP"| redis
  j3 -->|"429 if rate limited, buyer_id plus IP"| redis

  l1 -->|"try acquire or renew"| lock
  l2 -->|"try acquire or renew"| lock
  l3 -->|"try acquire or renew"| lock
  lock --> redis

  redis -->|"sorted set per product, ZADD score equals INCR"| admit
  lock -->|"only this tick's leader calls admit"| admit
  admit -->|"token SET EX 120s default, then PUBLISH"| redis

  c1 -->|"row lock plus idempotency key"| pg
  c2 -->|"row lock plus idempotency key"| pg
  c3 -->|"row lock plus idempotency key"| pg

  c1 -->|"201 reserved, 409 out of stock, 403 no token"| buyer
  c2 -->|"201 reserved, 409 out of stock, 403 no token"| buyer
  c3 -->|"201 reserved, 409 out of stock, 403 no token"| buyer

  prom -.->|"scrape /metrics on each replica"| r1
  prom -.-> r2
  prom -.-> r3
  prom --> graf
```

All three replicas run `_admission_loop` every `ADMISSION_TICK_SECONDS` (default 1s). Leadership is a Lua script: `GET` the lock, `SET key owner EX 5` if empty (and `INCR` the fencing epoch), `EXPIRE` to renew if this replica already holds it. Only the lock holder calls `admit_waiting_buyers`; that write re-checks the epoch in the same Lua as the queue pop. If the leader crashes or stops renewing, another replica's acquire succeeds once the TTL expires (observed failover time in testing: about 4 seconds). This is what stops three replicas from tripling the real admission rate to 60 buyers/sec instead of the intended 20.

What that maps to in the code:

- `POST /waiting-room/join` (`app/routes/waiting_room.py`) applies a Redis token-bucket limit (5 joins per minute per `buyer_id`, 20 per IP), then `ZADD`s the ticket onto a per-product sorted set (`app/waiting_room.py`).
- A background task in `app/main.py` sleeps `ADMISSION_TICK_SECONDS` (default 1) and calls `admit_waiting_buyers`, which pops up to `ADMISSION_BATCH_SIZE` (default 20) waiters per product, writes a short-lived admission token in Redis (default TTL 120 seconds), and `PUBLISH`es on `flashbuy:admission:{ticket_id}`.
- Leader election lives in `app/admission_leader.py`. Acquire/renew is one Lua script (`GET`, then `SET ... EX` or `EXPIRE`, default TTL 5s) plus an epoch `INCR` on a fresh acquire. `admit_waiting_buyers` re-checks that epoch in the same Lua as the sorted-set pop. `test_admission_leader.py` verifies both that three concurrent loops only ever admit at one replica's rate, and that a new leader takes over after the old one stops renewing.
- `GET /waiting-room/stream/{ticket_id}` is the primary wait path: SSE. It reads current status first (so a ticket already admitted is not missed), then subscribes to that ticket's channel. Heartbeats keep proxies from dropping an idle wait.
- `GET /waiting-room/status/{ticket_id}` is the documented **fallback** for clients or networks that cannot use SSE. It still returns live queue position, or the token once admitted. It is not removed.
- `POST /checkout` requires that token unless the internal header `X-FlashBuy-Test-Bypass: 1` is set (used by pytest and `scripts/prove_race_condition.py` only). It then `SELECT ... FOR UPDATE` on the product row, rejects with 409 if stock is 0, otherwise inserts a `reserved` order with a unique `idempotency_key` and decrements stock. A replay of the same key returns the original order and does not take another unit.
- `POST /orders/{order_id}/confirm` moves `reserved` to `confirmed`. A sweeper expires holds past `expires_at` (default 5 minutes from checkout) and returns the unit to stock.
- `GET /metrics` is scraped by Prometheus. Grafana is provisioned to use that Prometheus datasource and load the FlashBuy flash sale dashboard.

## How it was built

Phase 1 shipped a working but unsafe checkout: read stock, insert an order, decrement, with no lock. The point was a real oversell bug, not a stub.

Phase 2 added `scripts/prove_race_condition.py` (50 concurrent checkouts against stock 10). Against Phase 1 that run produced 50 successes and a remaining stock of 6, which is overselling. Checkout was then changed to `SELECT FOR UPDATE`, unique idempotency keys, `reserved` orders with `expires_at`, and a confirm plus expiry path. The same script then reported 10 successes and stock 0. Numbers are in `docs/race-condition-proof.md`.

Phase 3 put Redis in front of checkout. The waiting room is a sorted set plus a drip-feed admission loop. Join is rate-limited with an atomic Lua token bucket so one `buyer_id` or IP cannot occupy the whole line.

Phase 4 added `prometheus-client` metrics, a Prometheus scrape config, a pre-provisioned Grafana dashboard, and a Locust file that walks join, status poll, then checkout. A 2000-user Locust run was attempted. Locust reported CPU usage too high, status polls returned thousands of HTTP 500s, and join latency was dominated by that overload, so those percentiles are not used as results. The recorded run is 500 users, which is enough to exhaust stock 500 without saturating the load generator. That choice is documented in `docs/load_test_results.md`.

## Tech stack

From `requirements.txt` and `docker-compose.yml`:

| Piece | What it is used for |
| --- | --- |
| FastAPI, Uvicorn | Async HTTP API |
| Pydantic | Request and response models |
| SQLAlchemy (async) + asyncpg | PostgreSQL access |
| PostgreSQL 16 | Products and orders |
| Redis 7 | Waiting-room queue, admission tokens, rate limits |
| prometheus-client | `/metrics` |
| Prometheus v2.55.1 | Scrape and store metrics |
| Grafana 11.3.0 | Provisioned dashboard |
| Locust | Journey load test |
| pytest, pytest-asyncio, httpx | Automated tests |
| Docker Compose | Three app replicas, Caddy load balancer, Postgres, Redis, Prometheus, Grafana, Locust |

## How to run it locally

```bash
docker-compose up --build
```

If `docker compose up` fails with a port already allocated, something else on this machine is bound to a host port Compose wants. The known collisions are **5432** (Postgres) and **3000** (Grafana). Stop the other process or remap that port in `docker-compose.yml`. Grafana is not on the Locust path; Postgres is, so 5432 has to be free (or remapped) for tests that talk to the database from the host.

| Service | URL |
| --- | --- |
| API | http://localhost:8000 |
| OpenAPI docs | http://localhost:8000/docs |
| Prometheus metrics | http://localhost:8000/metrics |
| Grafana | http://localhost:3000 (user `admin`, password `admin`) |
| Prometheus UI | http://localhost:9090 |
| Locust UI | http://localhost:8089 |
| PostgreSQL | localhost:5432 (user `flashbuy`, password `flashbuy`, database `flashbuy`) |
| Redis | localhost:6379 |

On startup each app replica creates tables if needed and seeds one product if it is missing. Caddy listens on host port 8000 and round-robins to `app1`, `app2`, and `app3`. All three run the admission loop; only the Redis lock holder admits (Lua `SET ... EX` / `EXPIRE`, default 5s TTL, plus a fencing epoch). Without that lock, three replicas would admit 60 buyers/s instead of 20.

| Field | Value |
| --- | --- |
| id | `00000000-0000-4000-8000-000000000001` |
| name | Flash Deal Widget |
| stock | 500 |
| price_cents | 1999 |

Re-running compose does not reset that product's stock to 500.

## How to test it

### Automated tests

Tests run on every push and pull request through GitHub Actions using the isolated compose profile. That job starts a throwaway Postgres and Redis, **no** app replicas, so the fixtures can `drop_all` without wiping a live stack.

From `python-service/`:

```bash
docker compose --profile test run --rm --no-TTY test-runner
```

That starts `postgres-test` (host port 5433) and `redis-test` (host port 6380), runs `pytest -vv` in a one-off container pointed at those hosts (`REDIS_URL` uses Redis DB 15), then removes the runner. The same compose profile is used locally and in CI: `scripts/run_isolated_tests.sh` adds `--build`; CI runs the plain command with `--no-TTY`. After the tests, remove only the two test containers:

```bash
docker compose --profile test rm -sfv postgres-test redis-test
```

Do not run `docker compose --profile test down -v` on a machine with the live stack running, because it also removes the app, Postgres and Redis containers and their volumes. CI uses it only because the runner is throwaway.

Do not run pytest against the docker-compose stack while the app replicas are up. It shares the same database and will wipe it. Host `pytest` on default 5432/6379 is the same trap if `app1`/`app2`/`app3` are using those ports.

### Manual buyer flow

Admission is 20 tickets per product per second by default, so a single curl usually gets a token on the next status poll.

```bash
# 1. Join the line
curl -X POST http://localhost:8000/waiting-room/join \
  -H "Content-Type: application/json" \
  -d '{"product_id":"00000000-0000-4000-8000-000000000001","buyer_id":"buyer-1"}'
```

The response includes `ticket_id` and `position`. Extra joins from the same `buyer_id` are limited to 5 per minute.

```bash
# 2. Preferred: wait on the SSE stream until admitted (replace TICKET_ID)
curl -N http://localhost:8000/waiting-room/stream/TICKET_ID

# Fallback if SSE is blocked: poll until admitted is true and admission_token is set
curl http://localhost:8000/waiting-room/status/TICKET_ID
```

```bash
# 3. Checkout with that token (replace TOKEN)
curl -X POST http://localhost:8000/checkout \
  -H "Content-Type: application/json" \
  -d '{"product_id":"00000000-0000-4000-8000-000000000001","buyer_id":"buyer-1","idempotency_key":"attempt-1","admission_token":"TOKEN"}'
```

A successful checkout returns status `reserved`. Optional payment stand-in:

```bash
curl -X POST http://localhost:8000/orders/ORDER_ID/confirm
```

### Load test

`locustfile.py` creates a new product with stock 500 at the start of each run, then each simulated user joins, polls status about once per second until admitted or 3 minutes elapse, then calls checkout with the token.

Web UI: http://localhost:8089 after `docker-compose up`.

Headless command used for the recorded 500-user results:

```bash
docker compose run --rm locust locust -f locustfile.py --host http://lb:80 \
  --headless --users 500 --spawn-rate 25 --run-time 3m
```

Admission-rate times TTL stress (stock 50, delayed checkout so live tokens can pile toward 20/s × 120s = 2400). Stack must already be up; `compose run` without attaching to `python-service_default` cannot resolve host `app`:

```bash
docker compose up -d
docker run --rm -e LOADTEST_MODE=token_stress -e PYTHONUNBUFFERED=1 \
  --network python-service_default python-service-locust \
  locust -f locustfile.py --host http://lb:80 \
  --headless --users 5000 --spawn-rate 50 --run-time 10m
```

Same shape over SSE instead of status polling (`LOADTEST_MODE=token_stress_sse`):

```bash
docker run --rm -e LOADTEST_MODE=token_stress_sse -e PYTHONUNBUFFERED=1 \
  --network python-service_default python-service-locust \
  locust -f locustfile.py --host http://lb:80 \
  --headless --users 5000 --spawn-rate 50 --run-time 10m
```

## Results

Figures below are from the committed 2026-09-16 Locust run (repo commit `1e5fc1c`, three replicas behind Caddy, Locust 2.46.4 in the container). CSV and console log: [evidence/baseline_rerun_2026-09-16_clean/](evidence/baseline_rerun_2026-09-16_clean/). Machine and reproduce command: [docs/reproducibility.md](docs/reproducibility.md). Full tables, including why a 2000-user attempt was discarded, are in [docs/load_test_results.md](docs/load_test_results.md). The journal is [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md). Failure cases including fencing are in [docs/failure-modes.md](docs/failure-modes.md).

| | |
| --- | --- |
| Simulated users | 500 (spawn 25/s, 3 minutes) |
| Starting stock | 500 |
| Successful checkouts (HTTP 201) | 500 |
| Checkout failures | 0 |
| Final stock | 0 |
| Oversold units | 0 |
| `POST /checkout` p50 / p95 / p99 | 150 ms / 790 ms / 1.2 s (1200 ms) |
| `POST /waiting-room/join` p50 / p95 / p99 | 210 ms / 510 ms / 610 ms |
| Peak checkout RPS | 17.90 (admission cap 20 buyers/s) |

Peak checkout rate is bounded by that admission cap (`ADMISSION_BATCH_SIZE` default 20 per tick with `ADMISSION_TICK_SECONDS` default 1). It is not a throughput limit of the service.

Earlier run (2026-08-26, single Uvicorn worker, no committed CSV): `POST /checkout` p99 970 ms, peak checkout about 16.5 req/s. That day's Locust ticker also peaked at 204.00 aggregate req/s. That figure is mostly status polls, not checkout throughput, and not checkout capacity.

Chaos tests (Redis kill, replica kill, and related drills) are hand-run Docker drills documented in [docs/load_test_results.md](docs/load_test_results.md) (Phase 5B). They are not part of CI. The Redis kill lost waiting-room state and left 25 items unsold.

Concurrency correctness (50 concurrent checkouts, stock 10) is separate: Phase 1 oversold, Phase 2 did not. See [docs/race-condition-proof.md](docs/race-condition-proof.md).

## Limitations and what I would do differently

A 2,000-user Locust run was also executed. Locust reported that CPU usage was too high on the local machine, status polls returned thousands of HTTP 500s, and join latency went into the multi-second range. That is a load-generator and single-laptop limit, not a measured ceiling for the checkout path, so those percentiles are not reported as results.

A later `LOADTEST_MODE=token_stress` run (5000 users, stock 50, delayed checkout) was aimed at the admission-rate × TTL backlog: 20 admits/s × 120s TTL is 2400 live unused tokens in theory. Peak observed on 2026-09-01 was **1840** (the original 500-user run stayed around 20–40). HTTP 500s stayed at 0; about 22k HTTP 503s landed almost entirely on `GET /waiting-room/status`. Redis 1024 connections is enough for that many token keys, not for 5000 clients polling status about once a second. Locust warned that CPU was too high.

Replacing that poll with SSE (`LOADTEST_MODE=token_stress_sse`) on 2026-09-05, same 5000-user shape, produced **0 HTTP 503s** and **0 HTTP 500s**, peak live tokens **2160**, and **3460** concurrent open streams. Command-pool exhaustion from 1Hz polls is gone. The new bill is thousands of held HTTP and Redis SUBSCRIBE connections, plus a dedicated pub/sub pool of 6144. Locust did not print the CPU-too-high warning on the SSE run; it did on the polling re-run that same day (peak tokens 1899, 21221×503). Full table: [docs/load_test_results.md](docs/load_test_results.md).

With more time I would run the same journey on cloud VMs, with Locust workers on separate hosts from the API, until the system actually breaks. That is how you tell whether Postgres, Redis, or admission rate is the bottleneck. Compose now runs three app replicas behind Caddy; Redis leader election keeps admission at 20/s. Multi-instance Locust numbers are in [docs/load_test_results.md](docs/load_test_results.md). See also [docs/reproducibility.md](docs/reproducibility.md), [docs/failure-modes.md](docs/failure-modes.md), and [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md).

## License

MIT. See [LICENSE](LICENSE).
