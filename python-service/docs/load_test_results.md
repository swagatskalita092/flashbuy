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
204 database checkouts. Do not compare 204.00 to later checkout-only or
SSE-era rates. The figure that stays in the same 16–20 band across dates
is peak checkout RPS; see [reproducibility.md](reproducibility.md).

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

## 2026-09-05: polling vs SSE at 5000 users (Finding #3 follow-up)

Same machine, same compose stack, back to back. Original 2026-09-01 polling numbers above are **not** edited.

`GET /waiting-room/stream/{ticket_id}` is SSE: check status, subscribe to `flashbuy:admission:{ticket_id}`, re-check so a grant between those two steps is not lost, then wait for PUBLISH. Polling `GET /waiting-room/status/{ticket_id}` is unchanged.

### Side by side (5000 users, stock 50, spawn 50/s, 10 minutes)

| | Polling (2026-09-01, committed) | Polling (2026-09-05, this machine) | SSE `token_stress_sse` (2026-09-05) |
| --- | ---: | ---: | ---: |
| Peak live unused tokens | **1840** | **1899** | **2160** |
| HTTP 500 | **0** | **0** | **0** |
| HTTP 503 | **21977** | **21221** (21218 status, 3 checkout) | **0** |
| Peak open SSE streams | n/a | 0 | **3460** |
| Locust CPU-too-high warning | yes | yes | **not printed** |
| Final stock | 0 | 0 | 0 |

SSE checkout mix: 5000 checkouts, **4900×409** (stock 50), **0** stream failures, **0** 503s. Join 5000 / 0 failed.

**Did pool exhaustion go away?** For this 5000-user shape, **yes on HTTP 503s**: the command pool is no longer chewed by ~1Hz status GETs. Redis pub/sub uses a separate pool (`REDIS_PUBSUB_MAX_CONNECTIONS=6144`). The new cost is **thousands of held HTTP + SUBSCRIBE sockets** (gauge peak 3460). That is a different capacity curve, not "free." Locust's stream latency percentiles look like time-to-first-byte and are **not** used as wait-in-line time; the open-stream gauge is the concurrency signal.

Command for SSE (stack already up):

```bash
docker run --rm -e LOADTEST_MODE=token_stress_sse -e PYTHONUNBUFFERED=1 \
  --network python-service_default python-service-locust \
  locust -f locustfile.py --host http://lb:80 \
  --headless --users 5000 --spawn-rate 50 --run-time 10m
```

## 2026-09-05: Phase 5A three replicas behind Caddy

Goal: three FastAPI containers, Caddy round-robin on host `:8000`, Redis leader lock so admission stays 20/s (not 60/s), and a Locust 500-user stock-exhaust that still sells exactly 500.

### Wiring notes (observed, not assumed)

Nginx `upstream app1:8000` **exited on boot**: `host not found in upstream` because Compose started nginx before Docker DNS had `app1`. Switched to Caddy 2 (`Caddyfile`), which resolves backends at request time. `/health` then rotated `instance_id` app2 → app3 → app1, with **one** `admission_leader: true` (app3 at that moment).

Per-replica DB pools were cut to **15+20** so 3×80 does not exceed Postgres `max_connections=200`. Prometheus scrapes `app1/app2/app3` separately. Locust samples `/metrics` through the load balancer, so token/SSE gauges are whatever replica it hit that second (leader-only token recount). Treat those peaks as lower bounds, not a cluster sum.

### Finding: Caddy ate Locust's `X-Forwarded-For`

First 500-user run against `http://lb:80`, **before** the header fix. Product `0de6831c-a50a-4aad-beb4-ed38c5b429be`.

| | |
| --- | ---: |
| Join | 500 (**474× HTTP 429**) |
| Checkout HTTP 201 | **26** |
| Status | 26 (0 failed) |
| HTTP 500 / 503 | **0** |
| Final stock | **474** |
| Peak live tokens (LB `/metrics`) | **20** |
| Locust exit | 1 |

Caddy prepended the Locust container IP, so `client_ip()` (first `X-Forwarded-For` hop) saw one address. Join is 20/IP/minute. Locust idles on 429, so those buyers never entered the queue. **This is not oversell and not a 3× admission bug.** Peak tokens stayed 20 (a 3× drip would still have been starved of joiners). After the finding, Caddy `header_up X-Forwarded-For` passes Locust's per-user header through.

### Re-test: polling 500 users (after XFF fix)

`--users 500 --spawn-rate 25 --run-time 3m`, `--host http://lb:80`. Product `46aec72b-9430-46b3-b442-96d56039824e`.

| | |
| --- | ---: |
| Total HTTP requests | 2997 |
| Join | 500 (**0 failed**) |
| Status polls | 1997 (**0 failed**) |
| Checkout HTTP 201 | **500** |
| Checkout failures | **0** |
| HTTP 500 / 503 | **0** |
| Final stock | **0** |
| Oversold | **0** |
| Peak live tokens (LB `/metrics`) | **22** |
| Peak checkout RPS on Locust ticker | **19.80** (would be ~60 if all three replicas admitted) |
| Locust exit | 0 |

Latency (ms):

| Endpoint | p50 | p95 | p99 |
| --- | ---: | ---: | ---: |
| `POST /checkout` | 25 | 120 | 180 |
| `POST /waiting-room/join` | 62 | 160 | 240 |
| `GET /waiting-room/status` | 6 | 57 | 87 |

500 checkouts completed while users were still spawning (~20s spawn + a few seconds). That matches one 20/s valve, not three.

### Re-test: SSE 500 users through the same balancer

`LOADTEST_MODE=stock_exhaust_sse`, same 500/25/3m, product `90cc6755-ecd7-4a34-be61-e425c4435945`. Join, stream, and checkout each go through Caddy, so they routinely land on different replicas than the admission leader.

| | |
| --- | ---: |
| Join / stream / checkout | **500 / 500 / 500**, **0 failed** |
| HTTP 500 / 503 | **0** |
| Final stock | **0** |
| Oversold | **0** |
| Peak checkout RPS | **18.30** |
| Peak live tokens (LB `/metrics`) | **20** |
| Peak open SSE (LB `/metrics`) | **47** (per-replica gauge; not a cluster sum) |
| Locust exit | 0 |

Stream p50 was **7 ms** because many buyers connected after the leader had already granted; the stream handler's already-admitted check returns immediately. That is the cross-instance case working, not a broken wait.

Automated: `tests/test_admission_leader.py` runs three concurrent admit loops in one process; only one lock holder admits (40 waiters, 40 grants, one winner). Failover after TTL is a second test.

## 2026-09-05: Phase 5B chaos (kill one component mid Locust)

Same laptop, three app replicas + Caddy. Each scenario is its own `--users 500 --spawn-rate 25 --run-time 2m` against `http://lb:80`. Kill at ~8s into the run (during admission), restore after ~15–20s. Locust counts Caddy **502** as failures (not mapped 503). HTTP 500 stayed **0** in every run. Reservation TTL is 5 minutes, so `products.stock` later climbs back as the sweeper expires unpaid holds; order-row counts below are from `psql` after the runs.

### Finding: Caddy has no upstream health check; a dead replica is ~33% 502s

After pytest dropped tables, the three apps raced `CREATE TABLE`. `app3` exited: `UniqueViolationError` on `pg_type_typname_nsp_index` (`products`). Caddy kept round-robin to it. Unintended Locust (product `19ac1a90-050b-42dc-a540-087cd24a7be9`): **1282/3852 (33.28%) HTTP 502**, 0×500, 0×503. Locust final stock **0**. `psql`: **500** orders, **500** distinct idempotency keys. Restarting `app3` after the tables existed brought it back. Compose does not `restart: unless-stopped` on app replicas.

### Kill non-leader (`app2`), leader stayed `app1`

Product `7028f75c-b35b-48ac-a3ee-2cae7437cfac`. `docker kill` ~8s, `docker start` ~20s later.

| | |
| --- | ---: |
| Join | 552 (**52×502**) |
| Status | 1741 (**310×502**) |
| Checkout | 598 (**98×502**, rest 201) |
| HTTP 500 / 503 | **0** / **0** |
| Locust final stock | **0** |
| `psql` orders / distinct keys | **500 / 500** |
| Leader during outage | stayed **app1** |

Clients saw **raw 502** (Caddy, often multi-second waits up to ~5s), not graceful 503. Sale finished; no duplicate keys. Leader lock did not move, which is the point of killing a follower.

### Kill admission leader (`app1`)

Product `0cab0153-b6c0-404a-9a08-bd84c2db56b0`. Health still reported `admission_leader_id=app1` for a few seconds after kill (TTL). Then empty/502 from Caddy hitting the dead replica. At **~4s** after kill, `admission_leader_id=app2`. Within the 5s lock TTL. After `docker start app1`, app1 was follower.

| | |
| --- | ---: |
| Join | 533 (**33×502**) |
| Status | 2430 (**356×502**) |
| Checkout | 540 (**40×502**) |
| HTTP 500 / 503 | **0** / **0** |
| Locust final stock | **0** |
| `psql` orders / distinct keys | **500 / 500** |

Admission resumed on app2. No oversell, no duplicate orders. Hung-looking requests were Caddy 502 timeouts, not app hangs.

### Kill Redis ~15s

Product `a5fc588e-57fe-4f67-bfd8-fc85efea34ea`. Join completed (500, 0 failed) before or around the kill. Checkout **475×201, 0 failed**. Status: **441×503**, **2024×404**. Locust final stock **25**. `psql` later: stock **265** (sweeper had started releasing 5-minute holds), **475** orders, **475** distinct keys.

Redis restart **dropped waiting-room tickets** (status 404). Those buyers never checked out. **25 units unsold** at Locust shutdown. Not oversell; **lost admissions**. Mapped **503** during the outage (good). After Redis was healthy, `/health` still worked; leftover queue state did not come back. This laptop did not show hung checkouts (checkout p99 160ms) because most checkouts had already happened.

### Kill Postgres ~15s

Product `b37caa33-4a5a-4452-a356-04cfb3227b02`. Join **56×503**, checkout **70×503**, status **0 failed** (Redis still up). Checkout **max 18049 ms** (pool wait / connect, not a clean fail-fast). Locust final stock **0**. `psql`: **500** orders, **500** keys. HTTP 500 **0**, 503 **126**. After Postgres returned healthy, remaining checkouts completed. No duplicate keys. Worst client symptom was **multi-second to ~18s** checkout/join, then 503, then recovery.

Laptop caveat: these 2-minute 500-user kills ran on the same Windows Docker host as Locust. CPU was not the story; Caddy 502s and Redis/Postgres outages were.
