# Reproducing FlashBuy measurements

This file is the shortest path from a checkout of this repo to the
headline load-test commands, plus the machine those numbers were taken
on. It does not replace [load_test_results.md](load_test_results.md) or
[ENGINEERING_JOURNAL.md](ENGINEERING_JOURNAL.md). Those remain the
lab notes for every finding.

## Machine used for the 2026-09-16 baseline re-run

Collected on this host immediately before the re-run. The original
2026-08-26 write-up only said "one Windows development machine" and did
not record CPU or RAM, so this cannot be proven identical to that first
laptop from hardware fingerprints. Leftover Compose containers on this
host dated several days earlier match later documented runs (SSE,
chaos) on "the same laptop." Treat the specs below as the environment
for the **fresh 2026-09-16 baseline**, and as the best available
description of the Windows Docker laptop used for FlashBuy work.

| | |
| --- | --- |
| CPU | 11th Gen Intel Core i5-1135G7 @ 2.40 GHz |
| Cores | 4 physical / 8 logical |
| RAM | 15.76 GB |
| OS | Microsoft Windows 11 Home Single Language, 64-bit, build 10.0.26200 |
| Docker Desktop | 4.63.0 (4.63.0.220185) |
| Docker Engine | 29.2.1 (client and server) |
| Locust (in-container) | 2.46.4 |

All commands below are run from `python-service/`.

```bash
docker compose up -d
```

On this re-run, Grafana failed to bind host port 3000 (already in use).
That service is not on the Locust path. `app1` and `app3` also exited
once on the known startup `CREATE TABLE` race; they came up cleanly
after `docker compose start app1 app3`. Confirm `/health` on all three
replicas before starting Locust.

## 500-user baseline (stock exhaust)

Default `LOADTEST_MODE` (`stock_exhaust`). Locust creates a fresh
product with stock 500, 500 users join, poll status, then checkout.

```bash
docker compose run --rm locust locust -f locustfile.py --host http://lb:80 \
  --headless --users 500 --spawn-rate 25 --run-time 3m \
  --csv=results/baseline_rerun
```

The Compose `locust` service has no bind mount. `--rm` deletes
`/app/results` unless you add a volume, for example:

```bash
docker compose run --rm --no-TTY \
  -v "$(pwd)/evidence/baseline_rerun_YYYY-MM-DD:/app/results" \
  locust locust -f locustfile.py --host http://lb:80 \
  --headless --users 500 --spawn-rate 25 --run-time 3m \
  --csv=results/baseline_rerun --csv-full-history
```

On Windows PowerShell, capture the console with `Tee-Object`.
`--csv-full-history` writes per-endpoint RPS into the history CSV so
checkout throughput can be read separately from status polls.

### Fresh re-run: 2026-09-16 (clean)

Raw artifacts:

- [evidence/baseline_rerun_2026-09-16_clean/baseline_rerun_stats.csv](../evidence/baseline_rerun_2026-09-16_clean/baseline_rerun_stats.csv)
- [evidence/baseline_rerun_2026-09-16_clean/baseline_rerun_failures.csv](../evidence/baseline_rerun_2026-09-16_clean/baseline_rerun_failures.csv)
- [evidence/baseline_rerun_2026-09-16_clean/baseline_rerun_stats_history.csv](../evidence/baseline_rerun_2026-09-16_clean/baseline_rerun_stats_history.csv)
- [evidence/baseline_rerun_2026-09-16_clean/baseline_rerun_exceptions.csv](../evidence/baseline_rerun_2026-09-16_clean/baseline_rerun_exceptions.csv)
- [evidence/baseline_rerun_2026-09-16_clean/console.txt](../evidence/baseline_rerun_2026-09-16_clean/console.txt)
- [evidence/baseline_rerun_2026-09-16_clean/docker_stats.tsv](../evidence/baseline_rerun_2026-09-16_clean/docker_stats.tsv)

Product `147f4abc-4a13-4dc0-94db-1df508f4e403`. Stack at git `1e5fc1c`
(three FastAPI replicas behind Caddy). Locust exit **0**, no CPU
warning. `psql`: 500 orders, 500 distinct idempotency keys, stock 0.

| | |
| --- | ---: |
| Total HTTP requests | 3735 |
| Successful checkouts (HTTP 201) | **500** |
| Checkout failures | **0** |
| Join requests | 500 (0 failed) |
| Status polls | 2735 (0 failed) |
| HTTP 500 / 503 | **0** / **0** |
| Final stock | **0** |
| Oversold units | **0** |
| Peak checkout RPS (history ticker) | **17.90** |
| Peak aggregate RPS (history ticker) | 163.00 |
| End-of-test average RPS | 115.75 |

Latency (Locust, milliseconds):

| Endpoint | p50 | p95 | p99 | avg | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| `POST /checkout` | 150 | 790 | 1200 | 253 | 1306 |
| `POST /waiting-room/join` | 210 | 510 | 610 | 238 | 626 |
| `GET /waiting-room/status` | 55 | 310 | 440 | 104 | 561 |

Checkout p50/p95 match the 2026-08-26 headline (140/780) within normal
laptop variance. Correctness is the same: 500 reservations, stock 0, no
oversell.

## A note on comparing throughput across dates

The original 2026-08-26 **204.00 req/s peak aggregate RPS** figure was
measured on a **single Uvicorn worker**, before multi-instance Compose
existed. Phase 5A (three replicas, Caddy, Redis leader election) landed
on 2026-09-06 (`a36e2a0`). That 204.00 number was **dominated by cheap
status-polling requests** (3259 of 4259 total requests that day), not
checkout throughput. Peak checkout RPS that day was approximately
**16.5**.

After the SSE fix (2026-09-05, `c50b945`) and Phase 5A, the intended
client path no longer 1 Hz polls for admission. Later stress tests use
`LOADTEST_MODE=token_stress_sse` / `stock_exhaust_sse`. **Aggregate RPS
is therefore not a comparable metric** across these two points in the
project's history: the request mix changed, and 204.00 was never a
checkout-capacity number.

The metric that **is** comparable and stable is **checkout RPS**. It has
stayed in the 16-20 range throughout, because the waiting room
deliberately caps admission at **20 buyers/sec/product**. That is the
rate limiter working as intended, not a performance ceiling being hit.

| Run | Architecture | Peak checkout RPS |
| --- | --- | ---: |
| 2026-08-26 | Single worker, status polling | ~16.5 |
| 2026-09-05 | Three replicas + leader lock (XFF fix proof) | 19.80 |
| 2026-09-16 clean | Same 3-replica stack, polling baseline command | 17.90 |

This is a deliberate architectural choice: fairness and correctness
under load (one drip, no 3× admission, no oversell). It is not a
throughput limitation to be improved by adding replicas or removing the
waiting room.

Checkout latency across those same three runs, in milliseconds:

| Run | p50 | p95 | p99 |
| --- | ---: | ---: | ---: |
| 2026-08-26 (single worker) | 140 | 780 | 970 |
| 2026-09-05 (post-leader-lock) | 25 | 120 | 180 |
| 2026-09-16 clean | 150 | 790 | 1200 |

The 2026-09-05 proof run was faster; today's clean re-run sits next to
the original 2026-08-26 band. That is normal variance on one Windows
Docker laptop, not a regression in the checkout path.

## 5000-user SSE stress test

Not re-run as part of this file. Historical numbers (0 HTTP 503, 0 HTTP
500, peak live tokens 2160, 3460 open streams) are in
[load_test_results.md](load_test_results.md). Command:

```bash
docker compose up -d
docker run --rm -e LOADTEST_MODE=token_stress_sse -e PYTHONUNBUFFERED=1 \
  --network python-service_default python-service-locust \
  locust -f locustfile.py --host http://lb:80 \
  --headless --users 5000 --spawn-rate 50 --run-time 10m
```

The polling counterpart uses `LOADTEST_MODE=token_stress` with the same
user/spawn/run-time shape.

## Chaos tests (manual, not one command)

There is no Locust mode that kills infrastructure. Phase 5B started a
500-user, 2-minute run against `http://lb:80`, then killed one
component by hand about 8 seconds in and restored it after about
15-20 seconds. Recorded scenarios, from
[load_test_results.md](load_test_results.md):

1. Confirm three healthy replicas and note who is `admission_leader`.
2. Start Locust: `--users 500 --spawn-rate 25 --run-time 2m --host http://lb:80`.
3. About 8 seconds in, run **one** of:
   - `docker kill` the non-leader app container, then `docker start` it.
   - `docker kill` the current admission leader, then `docker start` it.
   - `docker kill` Redis, then start it again.
   - `docker kill` Postgres, then start it again.
4. After Locust stops, check Locust totals, `GET /health` leader id, and
   `psql` order count / distinct idempotency keys / stock.

Do not collapse those four kills into a single script and call it the
same evidence. Timing, which replica is leader, and whether join has
already finished all change the symptom.

## Historical runs without raw CSV

The 2026-08-26 baseline, the pool-exhaustion fix re-test, the 5000-user
SSE (and polling) stress runs, and the Phase 5B chaos runs are written
up in [ENGINEERING_JOURNAL.md](ENGINEERING_JOURNAL.md) and
[load_test_results.md](load_test_results.md) with method, product ids,
and tables. Those runs were **not** captured with Locust `--csv`, so
there are no committed stats/failures/history files for them. The
2026-09-16 clean baseline above is the first committed raw Locust CSV
set.

## Inspect the code that produced a finding

Checkout the SHA that recorded the work. Short SHAs as listed in the
journal appendix:

| SHA | What it records |
| --- | --- |
| `a512729` | Redis/Postgres pool exhaustion fix and mapped 503s |
| `3612c20` | Admission-rate × TTL token backlog stress (`token_stress`) |
| `c50b945` | SSE + Redis pub/sub replacing 1 Hz status polling |
| `a36e2a0` | Three replicas, Redis leader election, Caddy |
| `6090cfb` | Phase 5B chaos notes (Caddy 502s, failover, Redis ticket loss, Postgres 503s) |
| `da6ee3c` | Verified checkout / sweeper / SSE reconnect failure modes |
| `b4ef57c` | Architecture diagram and docs for the Phase 5A topology |

```bash
git show a512729
git checkout a36e2a0
```

The 2026-09-16 clean CSV re-run was taken at `1e5fc1c` (journal added;
no runtime change after `b4ef57c`).
