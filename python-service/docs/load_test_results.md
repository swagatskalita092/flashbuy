# FlashBuy load test results

Recorded **2026-08-26** against `docker compose` on one Windows development
machine (single Uvicorn worker, Postgres, Redis). Locust ran in Docker on
the same host.

Two runs. The 2000-user attempt is documented because that was the target;
its latency is **not** used as the headline number. Locust itself printed
`CPU usage was too high`, the API returned thousands of HTTP 500s on
status polls, and join p50 was 9.8s — that is the laptop saturating, not a
measured checkout-path cost. Using those percentiles on a resume would be
misleading.

The **500-user** run is the defensible one: enough buyers to exhaust
stock=500, Locust CPU stayed usable, and inventory stayed exact.

## Primary run (defensible): 500 users, spawn 25/s, 3 minutes

Product `71252f3d-1507-42cf-a310-caf107fc87c9`, starting stock **500**.

### Totals

| | |
| --- | ---: |
| Total HTTP requests | 4259 |
| Successful checkouts (HTTP 201) | **500** |
| Checkout failures | **0** |
| Join requests | 500 (0 failed) |
| Status polls | 3259 |
| Status HTTP 500 | 4 (0.12% of polls) |
| Rate-limited joins (429) | 0 |
| Out-of-stock checkouts (409) | 0 (buyer count = stock) |
| Final stock (`GET /products/{id}`) | **0** |
| Oversold units | **0** |

### Latency (Locust, milliseconds)

| Endpoint | p50 | p95 | p99 | avg | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| `POST /checkout` | 140 | 780 | 970 | 229 | 1089 |
| `POST /waiting-room/join` | 440 | 1300 | 1500 | 510 | 1556 |
| `GET /waiting-room/status` | 30 | 330 | 870 | 89 | 1042 |

### Throughput

Peak sustained aggregate request rate observed on Locust’s ticker:
**204.00 req/s**. End-of-test average over the full 3 minutes (including idle
after everyone had checked out): **129.09 req/s**. Checkout itself peaked
around **16.5 req/s**, in line with the admission drip (20/s cap).

## 2000-user attempt (not used for latency claims)

`--users 2000 --spawn-rate 50 --run-time 5m`, product
`fa7403c2-875f-49aa-905f-b1bbfe49232d`.

| | |
| --- | ---: |
| Total requests | 76608 |
| Checkouts | 1191 (**500** HTTP 201, **689** HTTP 409 sold-out, **2** HTTP 500) |
| Joins | 2034 (34 HTTP 500) |
| Status polls | 73383 (17784 HTTP 500, 50 disconnects, 4 resets) |
| Final stock | **0** |
| Oversold | **0** |
| Peak aggregate RPS (ticker) | 326.20 |
| Locust | warned CPU was too high |

Checkout p50/p95 in this run were 1300ms / 6400ms and join p50 was 9800ms.
Those figures track overload (500s + Locust CPU warning), so they are not
reported as the system’s normal p95.

## Interpretation

On this machine, the **waiting room + row lock** path sold every unit exactly
once: 500 reservations, stock 0, no oversell in either run. Checkout p95
stayed under **780ms** with 500 concurrent simulated buyers polling about
once a second. Admission (~20/s) is what bounds checkout RPS, which is the
point of Phase 3 — Locust’s 204 req/s peak is mostly cheap status polls, not
204 database checkouts.

Pushing to 2000 locust users on one box did not produce a more impressive
flash-sale number; it produced HTTP 500s and a Locust CPU warning. A
multi-core locust cluster and more API workers would be the next step before
quoting 2000-user latency.

## 2026-08-27: HTTP 500s on waiting-room under 500 users

### Finding

The original 500-user run had **4 HTTP 500s** on `GET /waiting-room/status` (0.12% of polls), clustered in short bursts. Join and checkout were otherwise clean. Those 500s were unhandled backend exceptions: FastAPI has no idea they were capacity issues, so Locust counted them as server bugs.

Likely cause: Redis was created with `Redis.from_url(...)` and **no `max_connections`**, relying on redis-py’s asyncio pool default (`max_connections or 2**31`, i.e. unbounded sockets). SQLAlchemy used the library default **`pool_size=5`, `max_overflow=10`** (15 Postgres connections). Join still does a product lookup in Postgres. Under 500 concurrent status polls plus spawn-time joins, bursts of Redis/Postgres timeouts and connection errors escaped the route handlers as generic 500s.

### Fix

- Redis pool explicitly sized to **1024** connections (500 pollers + joins + admission + headroom), with connect/read timeouts.
- Postgres pool **30 + overflow 50**, `pool_timeout=10`, `pool_pre_ping`, and server `max_connections=200`.
- Transient Redis/Postgres errors on join, status, and checkout (and `Depends(get_db)` via a FastAPI handler) now return **503** with a capacity message instead of 500.

### Re-test (same config, fresh volumes)

`docker compose down -v`, `docker compose up --build`, then:

`--users 500 --spawn-rate 25 --run-time 3m`

Product `37ae10da-cffd-43fe-9355-4690bfd6d94d`, starting stock 500.

| | |
| --- | ---: |
| Total HTTP requests | 10411 |
| Successful checkouts (HTTP 201) | **500** |
| Checkout failures | **0** |
| Join requests | 500 (**0 failed**) |
| Status polls | 9411 (**0 failed**, 0 HTTP 500, 0 HTTP 503) |
| Final stock | **0** |
| Oversold units | **0** |
| Locust exit code | 0 |

Latency (Locust, milliseconds):

| Endpoint | p50 | p95 | p99 | avg | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| `POST /checkout` | 560 | 2600 | 4400 | 825 | 6075 |
| `POST /waiting-room/join` | 1600 | 4700 | 5400 | 2068 | 5462 |
| `GET /waiting-room/status` | 350 | 1300 | 1600 | 424 | 1881 |

Peak aggregate RPS on the Locust ticker: **251.50**. End-of-test average over 3 minutes: **170.51 req/s**. Locust still printed a CPU-too-high warning on this laptop; that did not produce HTTP errors this time.

The waiting-room **500s are gone** (0/9411 status, 0/500 join). Latency is higher than the first 500-user run on a busier machine, so the original p50/p95 in the README stay the headline correctness numbers; this entry is specifically about failure rate after the pool fix.

## 2026-08-29: "Checkout 201 but seed stock still 500"

### Finding

A Locust run can show 500 HTTP 201s while `SELECT stock FROM products` on the **seed** row (`00000000-0000-4000-8000-000000000001`, Flash Deal Widget) still reads **500**. That looks like a failed commit. It is not.

`locustfile.py` inserts a **new** product (`Locust Flash SKU`, stock 500) at test start and only buys that id. The seed SKU is never in the waiting-room path. Querying it after the test will always show 500 unless someone checkouts that uuid.

Checkout already called `await db.commit()` before 201. Session close rolls back only *uncommitted* work. `pool_pre_ping` runs when a connection is checked out, not between decrement and commit. The 503 handler can wrap Redis failures *after* a successful commit; token consume is now best-effort so that cannot turn a sold unit into 503.

A first attempt to "prove" commit by requiring `SELECT stock` on a second connection to equal this request's expected remaining count was **wrong under concurrency**: the next checkout can commit first, stock is already lower, and this request returned HTTP 500 even though its order was committed (seen in an intermediate Locust run: 35 checkout 500s, Locust SKU stock still 0). The check is now "does this order id exist on a new connection?"

### Fix

- Explicit `rollback()` if commit raises anything other than handled `IntegrityError`.
- After commit, confirm the order row on a second connection; do not require stock to match a per-request expected value.
- Redis token cleanup cannot change a 201 into 503.
- Locust prints seed stock vs load-test product id so the two rows are not mixed up.
- Regression test: checkout then `SELECT stock FROM products` on a **new** SQLAlchemy session, assert stock decreased by 1 and one order row exists.

### Re-test (2026-08-29)

`pytest`: 14 passed (includes `test_checkout_commit_is_visible_on_a_new_db_connection`).

`scripts/prove_race_condition.py`: 10 successes, stock 0, no oversell.

Locust `--users 500 --spawn-rate 25 --run-time 3m` after rebuild:

| | |
| --- | ---: |
| Product | `b874b467-9b09-4597-9b4d-2e0178b65228` (Locust Flash SKU) |
| Checkout HTTP 201 | **500** (0 failures) |
| Join / status failures | **0** |
| Locust exit code | 0 |
| Checkout p50 / p95 / p99 | 45 ms / 300 ms / 410 ms |

Immediate `psql` (`SELECT id, name, stock FROM products`), same database, no reset in between:

| id | name | stock |
| --- | --- | ---: |
| `00000000-0000-4000-8000-000000000001` | Flash Deal Widget (seed) | **500** |
| `b874b467-9b09-4597-9b4d-2e0178b65228` | Locust Flash SKU | **0** |

`SELECT status, count(*) FROM orders WHERE product_id = 'b874b467-...'` : **500 reserved**. Seed stock 500 is expected. Load-test stock 0 matches 500 committed checkouts.

## 2026-09-01: admission-rate × TTL token backlog (`LOADTEST_MODE=token_stress`)

LinkedIn-style question: 20 admits/s × 120s TTL ⇒ **2400** live unused tokens if nobody redeems. The 500-user stock-exhaust run never sat there; stock matched demand and tokens were spent in seconds (live count stayed ~20–40).

This run used Locust mode `token_stress`: stock **50**, **5000** users (spawn 50/s, 10 minutes), 20% checkout within 8s of admission and 80% wait **85–115s**. Live tokens come from Prometheus `flashbuy_admission_tokens_outstanding` (Redis SCAN of `wait:token:*` every admission tick). Locust samples `/metrics` once a second.

Product `3c250a7a-1d12-4612-8ad3-e510477dd66c`. Locust on the compose network (`docker run --network python-service_default`). Locust printed **CPU usage was too high** (same laptop limit as the discarded 2000-user run).

| | |
| --- | ---: |
| Peak live unused tokens | **1840** (theoretical ceiling 2400) |
| HTTP **500** | **0** |
| HTTP **503** | **21977** (21975 status, 1 join, 1 checkout) |
| Join | 5001 (1×503) |
| Status polls | 317013 (**22576** failed: 21975×503, rest disconnects/resets) |
| Checkout | 5053 (50×201 implied by final stock 0, **4760×409**, 140×403 expired token, 1×503, some resets) |
| Final stock | **0** |

**Pools:** Postgres 30+50 did not produce checkout 500s at this token backlog. Redis **1024** is enough for ~1840 token *keys* but not for **5000 concurrent status pollers**; those 503s are the waiting-room Redis pool saying it is full, which is the mapping we added for Bug #1. Further Redis pool growth (or fewer pollers / slower poll) would be needed before claiming 5000 browsers at 1 Hz with no 503s. This run is valid for the token-ceiling question: 1840 is far above the original ~20–40.
