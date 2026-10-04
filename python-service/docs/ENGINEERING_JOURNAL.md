FlashBuy — Engineering Journal

A phase-by-phase record of how FlashBuy was designed, built, broken, fixed, scaled, and stress-tested. Every number in this document comes from an actual test run against this codebase; nothing here is a projection or an estimate presented as a result. Where a number is illustrative rather than measured, it is explicitly marked as such.

Table of Contents
Phase 0 — Problem Definition and Scope
Phase 1 — The Naive Implementation
Phase 2 — Correctness Under Concurrency
Phase 3 — The Waiting Room
Phase 4 — Observability and Load Testing
Interlude — Bug #1: Connection Pool Exhaustion
Interlude — Bug #2: The Post-Commit Verification Race
Finding #3 — The Admission-Rate × TTL Backlog
Finding #3 Follow-Up — Replacing Polling with SSE
Phase 5 — Multi-Instance, Chaos Testing, and Failure Modes
Phase 5.5 — Documentation Consistency Pass
Finding #4 — Frozen Leader and Fencing Tokens
The Two-Language Expansion
Future Work — Phase 6 (Not Yet Implemented)
Engineering Principles Applied Throughout
Appendix — Full Commit Timeline

<a name="phase-0"></a>

Phase 0 — Problem Definition and Scope
0.1 The real-world problem being modeled

Flash sales, limited ticket drops, and hardware restocks (PS5-style launches, Black Friday inventory drops, Ticketmaster-style on-sale moments) all share the same shape: a large number of buyers arrive within seconds of each other, competing for a small, fixed amount of stock. The system that mediates this has exactly three jobs, and all three have to hold at once:

Never oversell. If there are 500 units, at most 500 orders can succeed, no matter how many buyers hit the endpoint simultaneously.
Tolerate retries safely. Real clients retry on timeouts. A retried request must never result in a second unit being charged for the same purchase intent.
Stay responsive under load that is orders of magnitude larger than steady-state traffic. The system has to survive a spike, not just handle average load gracefully.
0.2 Why FlashBuy was scoped as two independent implementations

FlashBuy is deliberately structured as two unrelated codebases solving the identical problem under identical load-test conditions: python-service/ (FastAPI, complete) and java-service/ (in progress, built independently by a collaborator). This was not the original plan — it emerged from a practical constraint: a collaborator wanted to contribute but only in Java, and neither party wanted to learn the other's stack mid-project. Rather than force a compromise, the repository was restructured so each implementation stands alone, sharing nothing but the problem statement and the Locust test scenarios used to evaluate them. This means any future comparison between the two is a comparison of architecture and language, not of who-wrote-more-code.

0.3 Goals and explicit non-goals

Goals: correctness under concurrency, honest measurement (report what a test actually showed, including when a test had to be discarded), and treating operational failure (a dead dependency, a dead replica) as a first-class thing to test, not an afterthought.

Non-goals: this is not a production payments system, not a general-purpose e-commerce platform, and not an attempt to demonstrate every distributed-systems primitive that exists. Where a design decision was made to not add something — a message queue, Kubernetes, a saga orchestrator — that decision is recorded in this document with its reasoning, because "chose not to add X because Y" is itself part of the engineering record.

<a name="phase-1"></a>

Phase 1 — The Naive Implementation
1.1 Design intent

Phase 1's entire purpose was to build something convincingly wrong — a checkout endpoint that looks reasonable on a first read but fails under concurrency — and then prove, with a script, that it fails. Skipping straight to the "correct" implementation would have left no evidence that the correctness problem is real rather than theoretical.

1.2 What was built

A single checkout endpoint that: reads the current stock for a product, checks if stock > 0, inserts a new order row, and decrements stock by one — as three separate, unsynchronized database operations, with no row lock held across them.

1.3 Why this is broken

Between the read of stock and the write of the decrement, any number of other concurrent requests can perform the same read, see the same pre-decrement stock value, and proceed. This is the textbook "lost update" / race condition: correctness depends on operations being atomic that are not actually atomic.

1.4 The proof

scripts/prove_race_condition.py was built specifically to make this failure reproducible on demand: it fires 50 concurrent checkout requests against a product seeded with stock of 10.

Result against the Phase 1 implementation: 50 reported successes, final stock left at 6 — meaning 44 more units were "sold" than actually existed to sell, a direct, measured overselling failure, not a hypothetical one.

Commit: eef2cb1.

<a name="phase-2"></a>

Phase 2 — Correctness Under Concurrency
2.1 Row-level locking

The fix replaces the unsynchronized read-check-write with a single database transaction that opens with SELECT ... FOR UPDATE on the product row. This forces every concurrent checkout attempt against the same product to serialize at the database level — only one transaction can hold the lock on that row at a time, so the read-then-decrement sequence becomes effectively atomic from the point of view of any other transaction.

2.2 Idempotency keys

Locking solves concurrent-request correctness, but not retry correctness. A client that times out waiting for a response has no way to know whether its request actually succeeded server-side. If it retries blindly, a naive system would create a second order and decrement stock a second time for what was, from the buyer's perspective, one purchase attempt.

The fix: every checkout carries a client-supplied idempotency key, enforced as a unique constraint on the orders table. A retried request with the same key does not create a second row — it returns the original order. This was later hardened further after Bug #2 (see below) changed how success is verified, but the core mechanism — a unique constraint doing the enforcement, not application-level "check then insert" logic that could itself race — was established here.

2.3 Reservation expiry

A successful checkout does not immediately finalize a sale — it creates a reserved order with a 5-minute expiry window (expires_at), modeling the real-world need for a payment step between "claimed" and "confirmed." A background sweeper job periodically finds reservations past their expiry and releases the held stock back to the pool, so an abandoned checkout does not permanently lock inventory that could have gone to another buyer.

2.4 Re-running the proof

scripts/prove_race_condition.py, run again against the Phase 2 implementation under the same 50-concurrent-request, stock-of-10 scenario: 10 successes, stock left at exactly 0. No oversell, no undersell.

Commit: 10c98e2.

<a name="phase-3"></a>

Phase 3 — The Waiting Room
3.1 Why row locking alone doesn't scale

SELECT FOR UPDATE guarantees correctness, but it does so by serializing access to a single database row. If 50,000 buyers hit checkout in the same second, the database becomes a queue whether anyone designed it to be one or not — except it's an invisible, unmanaged queue with no fairness guarantee and no backpressure, and it will happily let the connection pool exhaust itself trying to serve all 50,000 at once (a failure mode that, in fact, later showed up for real — see Bug #1).

The fix is to build the queue on purpose, in front of the database, rather than let the database become one by accident.

3.2 The queue itself

A Redis sorted set holds, per product, a FIFO ordering of waiting buyers (ZADD, scored by arrival time). This is intentionally simple — no priority tiers, no VIP lanes — matching the actual problem (fair access to scarce inventory) rather than a more general queueing system.

3.3 The admission loop

A background task, running on a fixed interval (default: every 1 second), pops a bounded batch of waiters off the front of the queue (default: 20 per product per tick) and "admits" them — meaning it grants each one a short-lived admission token. This converts an unbounded concurrent stampede into a controlled drip-feed that the checkout path (and the database behind it) can actually absorb.

3.4 Rate limiting via Lua

To stop one aggressive client from occupying a disproportionate share of the queue, joins are rate-limited per buyer ID and per IP using a token-bucket algorithm implemented as an atomic Redis Lua script. The atomicity matters here for the same reason row locking mattered in Phase 2: a check-then-increment counter implemented as two separate Redis calls would itself be racy under concurrent joins from the same client.

3.5 Admission tokens

Once admitted, a buyer receives a token with a 120-second TTL, which checkout requires as proof of admission. This is the mechanism that later became the subject of Finding #3 (below) — the interaction between the admission rate and this TTL turned out to have a non-obvious worst case that wasn't visible in the original test scenario.

Result at the time: 12 tests passed. No commit-level load test yet — that arrives in Phase 4.

<a name="phase-4"></a>

Phase 4 — Observability and Load Testing
4.1 Metrics

A /metrics endpoint, scraped by Prometheus, exposes counters and gauges for queue depth, admission rate, checkout outcomes, and (later) SSE connection counts and outbox-style internals as they were added.

4.2 Dashboard

A pre-provisioned Grafana dashboard visualizes the Prometheus data without requiring manual panel setup — the dashboard ships with the repo and comes up correctly on docker compose up.

4.3 The Locust scenario

The load test does not merely hammer the checkout endpoint. It simulates the full buyer journey a real user would experience: join the waiting room, poll (or later, stream) for admission status, then attempt checkout once admitted. This matters because a load test that skips the waiting room entirely would be testing a different, easier system than the one that actually ships.

4.4 The recorded baseline: 500 concurrent users

Spawn rate 25/s, 3-minute run, against a product seeded with 500 units of stock.

Results: 500 successful checkouts, final stock exactly 0. Checkout latency: p50 140ms, p95 780ms, p99 970ms. Peak aggregate throughput: 204.00 requests/second. That 204.00 figure is Locust's combined request rate, mostly 1 Hz status polls (3259 of 4259 requests that day), not checkout throughput. Checkout itself peaked around 16.5 req/s, in line with the 20/s admission cap. It should not be compared to later checkout-only or SSE-era rates; the stable figure across dates is checkout RPS in the 16-20 range (see [reproducibility.md](reproducibility.md)). Of the status polls, 4 (0.12%) returned HTTP 500 — a number that, at the time, was accepted as noise, but which later investigation (Bug #1, below) traced to a real and fixable cause rather than genuine noise.

4.5 The discarded 2,000-user run

A larger run at 2,000 simulated users was attempted. The product sold out cleanly, but Locust itself reported CPU saturation on the load-generation side, and status-poll latency became dominated by the load generator's own overload rather than the system under test. Rather than report those latency numbers as if they reflected FlashBuy's actual performance, they were explicitly discarded and documented as invalid — a decision that set the pattern for every subsequent test in this project: a number that cannot be trusted is not reported as if it can be.

Commit: 0f9a857.

<a name="bug-1"></a>

Interlude — Bug #1: Connection Pool Exhaustion

Discovered: August 27, 2026, during the 500-user baseline analysis above.

Symptom

442 failures out of 9,963 total requests (4.4%), overwhelmingly concentrated on the /waiting-room/status polling endpoint.

Root cause

Two independent resource ceilings were being hit simultaneously: the Redis connection pool had no explicit cap (so it could be exhausted silently), and the Postgres pool was undersized for the polling volume a real admission-loop scenario generates.

Fix

Redis pool explicitly capped at 1024 connections. Postgres pool set to 30 base connections plus 50 overflow, with pool_pre_ping enabled to detect and discard dead connections before they cause a failure further downstream. Postgres server-side max_connections raised to 200. Critically, the fix also changed how the system fails when it does hit a ceiling: transient capacity errors now return HTTP 503 (service temporarily unavailable, safe to retry) rather than HTTP 500 (generic server error, ambiguous to a client), a change implemented in app/capacity.py.

Verification

Re-run of the 500-user scenario: 0 HTTP 500s, 500/500 checkouts, 13 tests passing.

Commit: a512729.

<a name="bug-2"></a>

Interlude — Bug #2: The Post-Commit Verification Race

Discovered: August 29, 2026.

Symptom

35 false HTTP 500 responses in a single test run, on checkouts that had, in fact, succeeded — the order was correctly created, but the client was told it had failed.

Root cause

A post-commit sanity check re-read the product's stock and compared it against a value the request had computed as "expected" earlier in its own execution. Under concurrency, another buyer's checkout could legitimately commit in the gap between this request's own commit and its post-commit check, changing the stock value out from under the check and causing it to fail — even though this request's own order was entirely correct.

A wrong diagnosis, caught before it shipped

The first attempt at diagnosing this bug checked the stock of the wrong product — the original seeded row rather than the specific SKU the Locust run had actually been exercising — and drew an incorrect conclusion as a result. This was caught before a fix was shipped based on the wrong diagnosis, and the record is kept here deliberately: the practice adopted afterward ("always confirm which specific row a test actually targeted before concluding something is broken") traces directly back to this near-miss.

Fix

Success verification was changed to check for the existence of the order row itself, read on a fresh database connection, rather than comparing against a stock value computed earlier and vulnerable to being invalidated by unrelated concurrent activity.

Verification

14 tests passing, prove_race_condition.py at 10/10, Locust run at 500/500 checkouts with 0 failures, cross-checked directly against the database via psql on the correct product this time. A six-part pre-push audit (concurrency behavior, resource handling, test coverage, documentation accuracy, secrets handling, general code health) was run before this fix shipped — the first time that audit discipline was applied in this project, and it became standard practice for every subsequent change.

Commit: e20d769.

<a name="finding-3"></a>

Finding #3 — The Admission-Rate × TTL Backlog

Prompted by: a LinkedIn comment (from a commenter identified as Edilec) on a post describing Bugs #1 and #2.

3.1 The claim being tested

The commenter pointed out something the original 500-user test had never actually exercised: the real worst-case number of simultaneously outstanding admission tokens is bounded by admission rate × token TTL. At 20 admissions/second and a 120-second TTL, that ceiling is 2,400 tokens outstanding at once — and the 500-user baseline test never got close to that state, because its queue fully drained in roughly 25 seconds, well inside the TTL window. In other words, the existing test had been validating a scenario that never stressed this particular mechanism.

3.2 The test built to check it

A new Locust mode, LOADTEST_MODE=token_stress: stock of 50, 5,000 simulated buyers, with 80% of them deliberately holding their admission token for 85–115 seconds before attempting checkout — engineered specifically to push the system toward the theoretical 2,400-token ceiling.

3.3 Results

Peak outstanding tokens observed: 1,840 (against a theoretical ceiling of 2,400, and against just 20–40 outstanding tokens in the original 500-user baseline — confirming the original test genuinely had never touched this regime). Zero HTTP 500 responses. 21,977 HTTP 503 responses, almost entirely on status polling — which, on investigation, was the Bug #1 graceful-degradation path (Redis pool exhaustion under roughly 1Hz polling from 5,000 simultaneous clients) working exactly as designed, not a new failure. Locust again hit CPU limits during this run, so the exact 503 count is reported as directional rather than precise, the same honesty standard applied to the discarded 2,000-user run in Phase 4.

3.4 A near-miss on verification

This work initially existed only as uncommitted local changes and was, at one point, reported as complete when it was not actually pushed to GitHub — caught by a direct repository check that showed the work missing from the remote. It was then genuinely committed, pushed, and re-audited to confirm its presence. This is the one occasion in this project's history where a self-reported "done" turned out to be inaccurate, and it is the reason "always verify against a fresh clone, never trust a summary" became a standing rule rather than an occasional precaution.

Commit: 3612c20. A reply was posted to the original LinkedIn comment using these GitHub-verified numbers.

<a name="finding-3-followup"></a>

Finding #3 Follow-Up — Replacing Polling with SSE
Why polling itself was the actual bottleneck

Finding #3 confirmed the admission mechanism was working correctly under stress — but it also made visible that the status-checking mechanism (polling) was the actual weak point: 5,000 clients polling roughly once per second is 5,000 requests/second of pure overhead, unrelated to whether anyone is actually being admitted.

The fix: Server-Sent Events over Redis pub/sub

The admission loop was changed to publish a message on a per-ticket Redis channel the moment a buyer is admitted. A new endpoint, GET /waiting-room/stream/{ticket_id}, checks the buyer's current status first (to correctly handle the case where admission happened before the client subscribed — a real race that had to be explicitly designed around, not an edge case that could be ignored), then subscribes to that buyer's channel and pushes exactly one event when it arrives. The original polling endpoint was kept, deliberately, as a documented fallback for clients or network conditions that cannot use SSE — this was not a wholesale replacement, but an additive improvement with a safety net.

Before/after: identical 5,000-user, stock-50, 10-minute scenario
Metric	Polling (baseline)	Polling (re-run)	SSE
Peak live tokens	1,840	1,899	2,160
HTTP 500	0	0	0
HTTP 503	21,977	21,221	0
Peak open SSE connections	n/a	0	3,460
The honest cost

SSE does not make the resource problem disappear — it converts it into a different one. Instead of thousands of HTTP 503s from an overwhelmed Redis connection pool, the system now holds thousands of long-lived open connections (peak observed: 3,460), which cost memory and file descriptors instead of CPU and connection-pool contention. This tradeoff is documented explicitly in the README rather than presented as if SSE were a strictly-better free improvement, because it isn't — it moves the bottleneck, and a production deployment would need to actually plan for that different resource cost.

Test suite grew from 15 to 17 functions to cover the new endpoint and its already-admitted race condition.

Commit: c50b945.

<a name="phase-5"></a>

Phase 5 — Multi-Instance, Chaos Testing, and Failure Modes

This phase is the largest single body of work in the project and is broken into three sub-phases, each independently verified against a fresh clone of the repository.

5A — Three Replicas and Leader Election
5A.1 The problem anticipated before it was built

Before writing any multi-instance code, the question asked was: what assumption does the single-instance admission design quietly depend on? The answer: it assumes there is exactly one admission loop running. Naively running three replicas of the app behind a load balancer would mean three independent admission loops, each admitting 20 buyers/second, silently tripling the real admission rate to 60/second — breaking a rate limit that the entire waiting-room design exists to enforce. This was identified and designed around before implementation, not discovered afterward by a load test catching the discrepancy — a deliberate departure from the earlier pattern (where bugs were mostly found after the fact) toward anticipating a class of problem before it ships.

5A.2 The leader election mechanism

A Redis SET NX with a 5-second TTL is used to elect a single leader among the three replicas. Only the replica currently holding the lock runs the admission loop on a given tick; the other two stand by. If the leader stops renewing the lock (crash, network partition, or graceful shutdown), the lock expires and another replica acquires it, becoming the new leader.

5A.3 Verification

A dedicated test, test_three_admission_loops_do_not_triple_the_drip, runs three concurrent admission loops against a shared Redis instance and asserts that only one of them ever actually admits buyers in a given tick, with the combined admission rate matching a single replica's rate rather than three times it. A second test verifies failover: the leader stops renewing, and another replica takes over once the TTL expires.

5A.4 A real bug found during this phase's own load testing

The first 500-user Locust run through Caddy (the newly-introduced load balancer) produced 474 HTTP 429 (rate-limited) responses on join and only 26 successful checkouts — a result that looked like the admission system had failed. Root cause: Caddy was forwarding the Locust container's IP address rather than each simulated buyer's distinct IP, which meant the per-IP rate limiter saw 500 simulated buyers as a single client and rate-limited nearly all of them. This was fixed by correctly propagating Locust's X-Forwarded-For header through Caddy.

After the fix: polling scenario — 500/500 checkouts, stock exactly 0, zero failures, peak checkout throughput 19.80 requests/second (matching the intended single-replica rate, confirming the leader lock is doing its job rather than allowing tripling). SSE scenario — 500/500/500 across join, stream, and checkout, stock exactly 0, stream latency p50 of 7 milliseconds.

Commit: a36e2a0.

5B — Chaos Testing

Each of the following was tested by deliberately killing one component during a live 500-user run and recording what clients actually experienced — not what the design predicted they would experience.

Component killed	What clients actually saw	Did it recover?	Final order integrity
A non-leader app instance	~33% of requests returned HTTP 502 (Caddy has no upstream health check to route around a dead node)	Leader instance unaffected; stock correct	500/500 keys, zero duplicates
The leader instance	Brief HTTP 502 window, then a new leader took over in approximately 4 seconds (inside the 5-second lock TTL)	Yes	500/500 keys
Redis (~15-second outage)	Status endpoint returned 503s and 404s; checkout itself continued succeeding (475 successful checkouts during the outage)	Ticket/queue state for that window was lost — roughly 25 units went unsold as a direct consequence — but zero units were oversold	475 keys, zero duplicates
PostgreSQL (~15-second outage)	Join and checkout requests returned 503s; checkout latency spiked to as high as ~18 seconds during recovery	Yes, stock correct once Postgres came back	500/500 keys

An unplanned, genuinely accidental crash also occurred during this phase's own test execution: one app replica (app3) died from a real Postgres CREATE TABLE race condition triggered during pytest, producing 1,282 HTTP 502 responses out of 3,852 requests in that run. This was not a scenario anyone deliberately engineered — it happened by accident during testing — and it is reported here for the same reason the discarded load-test runs are reported: it happened, so it is documented, rather than quietly excluded because it wasn't the test that was planned.

The honest headline result: across every single scenario in this table, zero duplicate idempotency keys were ever created — correctness held throughout every failure mode tested. But Redis's loss of in-flight queue and ticket state during an outage is a real, acknowledged weak point of the current design, not something glossed over in the writeup.

Commit: 6090cfb.

5C — Documented and Verified Failure Modes

Three specific failure modes were identified, deliberately triggered (not merely reasoned about on paper), and documented in python-service/docs/failure-modes.md:

5C.1 — The expiry sweeper stopping. If the background sweeper that releases expired reservations stops running, held stock stays deducted indefinitely rather than returning to the pool. Verified with a dedicated test. Because all three replicas each run their own sweeper, and SKIP LOCKED prevents them from fighting over the same rows, this failure mode requires all app processes to stop — a single replica's sweeper dying is not sufficient to trigger it, which is itself a useful property to have proven rather than assumed.

5C.2 — Client timeout followed by retry with the same idempotency key. Verified to correctly return the original order with exactly one unit charged, via the existing duplicate-key test. An honest limitation is noted alongside this: a client that times out, then retries with a new idempotency key (rather than the same one) after the original request actually succeeded server-side, would represent a real double-charge risk — and the current Locust test harness's retry behavior does not exercise this specific scenario. This gap is recorded rather than silently left untested and unmentioned.

5C.3 — SSE publish with no subscriber. If a buyer is admitted while momentarily disconnected from their SSE stream, the already-admitted status check on (re)connection catches this correctly — verified both by a dedicated unit test and by the SSE Locust run's 7ms stream p50 latency under real load.

Commit: da6ee3c.

<a name="phase-5-5"></a>

Phase 5.5 — Documentation Consistency Pass

Date: September 11, 2026.

Following the completion of Phase 5A, the README's architecture diagram still depicted the earlier single-instance topology, with no Caddy, no replicas, and no leader-election mechanism shown anywhere — a genuine documentation gap between what was built and what was described, not a cosmetic omission.

What changed

The intro paragraph above the architecture diagram was rewritten to describe Caddy load-balancing across three replicas with Redis-enforced single-leader admission. The Mermaid diagram itself was rebuilt to show Caddy as the entry point, three FastAPI replica subgraphs, the SET NX / 5-second-TTL leader lock, and the leader-only admission loop feeding into the existing Redis/Postgres/Prometheus/Grafana components. A new prose paragraph explains the failover behavior in plain language, including the ~4-second observed failover time measured during Phase 5B's chaos testing. A new bullet point documents app/admission_leader.py and its accompanying test, test_admission_leader.py.

An incidental environment issue, correctly triaged

Running the existing test suite after this documentation change initially produced 20 connection-refused errors across the board. Rather than assume the documentation edit had somehow broken something (a README change touching no application code, which should have been an implausible cause on its face), a systematic check was run: the test configuration's connection strings were confirmed correct (localhost, matching the Compose port mappings, not a Docker-internal service name), a possible duplicate tests/ directory was ruled out, and Docker's own port-publishing state was inspected directly — which revealed the actual cause: Docker Desktop reported the Postgres and Redis containers as "healthy," but had not actually bound their ports to the host, a known glitch that can occur after a Docker Desktop restart while old containers remain "up." Recreating the two services (docker compose up -d --force-recreate postgres redis) restored the port bindings, and the full 20-test suite then passed cleanly. This is recorded here as an example of correctly separating "did my change cause this" from "is something else going on," rather than reflexively reverting or re-litigating a correct documentation change because of an unrelated environment hiccup.

Commit: b4ef57c. Verified directly against a fresh clone of origin/main — diagram, prose, and code bullet all confirmed present exactly as intended.

<a name="finding-4"></a>

Finding #4 — Frozen Leader and Fencing Tokens

Question asked: what happens if the admission leader does not die, but freezes longer than its Redis lease and then wakes up?

Limitation of what we had already tested: Phase 5B only kills processes. `docker kill` / `docker stop` never leave a process that already passed `hold_admission_leadership` and still has `admit_waiting_buyers` left to run. A GC pause, `kill -STOP`, or a frozen VM is that case. The lease check and the queue write were two Redis round trips. After a freeze in between, another replica can take the lock; the resumed process still admits with a stale "I am leader" answer. Two drip-feeds in one window, which is the 3x-rate failure Phase 5A existed to prevent. A lease alone cannot survive that, because check-then-act across two Redis calls is never atomic.

How it was found: outside feedback (Edilec on LinkedIn), not an internal chaos run. Same class of story as Finding #3: the gap was real in the code, and the existing tests did not cover it.

Fix: a fencing epoch (`INCR` of `flashbuy:admission:leader_epoch` only on a new acquire, never on renew). Every admit Lua GET of that key must match the epoch from this tick's leadership result, in the same script as ZRANGE/SET/HSET/ZREM. Stale epoch returns **-1** (empty queue returns 0). The loop logs `admission rejected: stale epoch ...` so a live freeze is visible in `docker logs`.

Verification: `test_stale_fencing_epoch_is_rejected_after_a_simulated_freeze` deletes A's lease (stand-in for TTL while frozen), lets B admit on a new epoch, then A calls admit with the old epoch and must get -1. The test was confirmed to fail when the Lua epoch check is removed (`assert 5 == 0`), then pass with the check restored. Live `docker pause` of the Locust-era leader confirmed leadership transfer (app3/epoch 1 → app2/epoch 2, 500 orders / 500 keys, no split-brain on `/health`). Direct stale-write rejection is proven by that unit test and the -1/log signal, not by a line in the live drill's app3 logs. The same drill also showed Caddy holding requests on the frozen replica for most of a minute; that timeout gap is recorded, not fixed here.

<a name="two-language"></a>

The Two-Language Expansion
Motivation

A collaborator wanted to contribute to FlashBuy but only in Java, while the primary author had no Java experience and no interest in acquiring it mid-project; the collaborator, in turn, had no Python experience. Rather than force either party to work outside their comfort zone, or attempt some hybrid shared-code compromise that would satisfy neither stack fully, the repository was restructured into two fully independent implementations of the identical problem.

Ground rules

Neither implementation borrows code, patterns, or infrastructure from the other. No shared database, no shared runtime, no hybrid request flow between them. The only thing they share is the problem statement and the intent to eventually be evaluated under identical Locust load-test scenarios, so that any future comparison is a comparison of architectural and language choices — not a comparison confounded by one implementation reusing the other's groundwork.

Current status

python-service/ is complete, as documented across Phases 1 through 5.5 above. java-service/ remains a placeholder README ("Java implementation in progress") — the collaborator has not yet begun active work. The complete problem breakdown (row-level locking and idempotency design, the waiting-room and rate-limiting design, the push-based admission design, and now the multi-instance leader-election design) is queued to be handed off once that work begins in earnest.

<a name="phase-6"></a>

Future Work — Phase 6: Durable Event Delivery (Not Yet Implemented)

Status: designed, not started. This section documents a plan, not a shipped feature. No code referenced below exists in the repository yet.

Why this is being recorded now, before any of it is built

The rest of this document describes work that has already been built, measured, and verified. This section is different in kind: it is a considered engineering plan for a genuinely new distributed-systems problem that FlashBuy, as it currently stands, does not touch — kept here so the reasoning behind it is not lost, and so a future return to this work starts from a real design rather than a blank page.

6.1 The problem this phase would address

Every phase built so far solves problems that live inside a single service's boundary: protecting one service's own database from its own concurrent requests. Phase 6 would introduce a second, independent service — a Fulfillment Service — that needs to reliably learn when an order transitions from reserved to confirmed, without being in the same database transaction as the order itself. The moment two independent services need to agree on "did this happen," a much harder class of problem appears: the message announcing the event can be lost (if the publishing process crashes between committing the order and sending the message) or delivered more than once (if a retry fires after an ambiguous failure), and the receiving service must handle either outcome without either silently dropping a real order or duplicating its own side effect (in this case, creating two fulfillment jobs for one order).

6.2 Why Fulfillment was chosen over Payment as the second service

A payment-service integration was considered and deliberately rejected for this phase. Payment introduces a return path (the order service needs to learn the outcome back from the payment service), which drags in failure compensation, refunds, and effectively the saga pattern — a substantially larger and different project. Fulfillment is one-directional (order confirmed → fulfillment job created) and isolates exactly the property worth proving: a committed database change must eventually cause exactly one logical downstream effect, despite unreliable message delivery.

6.3 The core mechanism: transactional outbox

Rather than committing an order and separately attempting to publish an event (two operations that can fail independently, leaving the system in an inconsistent state if the process dies between them), the order-confirmation transaction would also insert a row into an outbox_events table, in the same database transaction. A separate publisher process would then read pending outbox rows and actually deliver them to a message broker, retrying on failure, only marking a row as published once the broker has confirmed receipt.

6.4 Broker choice: RabbitMQ over Kafka

RabbitMQ was the planned choice specifically because its two explicit guarantees — publisher confirms (the broker has accepted responsibility for a message) and manual consumer acknowledgements (the consumer has finished processing a message) — map directly onto the two failure boundaries this phase exists to demonstrate. Kafka would work, but introducing it would pull the explanation toward partitions, consumer groups, and offset management — real concepts, but not the ones this phase is trying to isolate and prove.

6.5 The consumer side: an idempotent inbox

The Fulfillment Service, on the receiving end, would record every event ID it has processed in an inbox_events table, in the same transaction that creates the resulting fulfillment job. A duplicate delivery of an already-processed event would be detected and safely ignored rather than creating a second fulfillment job — the property that makes "at-least-once delivery" survivable rather than dangerous.

6.6 Planned failure-injection scenarios

Mirroring the chaos-testing discipline established in Phase 5B, the plan calls for deliberately: killing the publishing process after a database commit but before the event is published (reproducing the classic "dual-write" data-loss bug, on purpose, as a baseline before the fix); killing the publisher after the broker has confirmed receipt but before the local outbox row is marked published (deliberately causing a duplicate delivery, to prove the consumer handles it); killing the consumer after it commits its own database transaction but before it acknowledges the broker (again deliberately causing redelivery); simulating a sustained broker outage while checkouts continue; and sending a deliberately malformed "poison" event to confirm it is routed to a dead-letter queue without blocking unrelated valid events behind it.

6.7 What "done" would mean

This phase would not be considered complete merely because messages are flowing between two services. The explicit bar, carried over from how every other phase in this project has been judged: order state and its corresponding event must be provably atomic (one database transaction, not two operations that can diverge); the system must survive the broker being down without losing a single committed event; deliberately-caused duplicate deliveries must be proven, via an actual test run with actual numbers, to never result in a duplicate fulfillment job; and the README for this phase must say, explicitly, "at-least-once delivery, not exactly-once" — because claiming exactly-once delivery across two independent services would not be an honest description of what this architecture actually provides.

6.8 Why this is being deferred rather than started now

Weighed against this project's actual purpose — supporting a job search with a finite timeline — the remaining highest-leverage work on FlashBuy is not more engineering. It is: getting the already-complete, already-verified work represented accurately on an actual resume document, getting it integrated into an actual portfolio site, and being able to explain the decisions already made (leader election, the SSE tradeoff, the honestly-reported Redis chaos-test weak point) out loud, without the README open, in an interview. Phase 6 is realistically an 8-to-12-focused-day undertaking once started, given the same standard of chaos-testing and honest measurement applied to everything else here — a genuine and valuable extension, but one that should follow the lower-effort, higher-immediate-payoff work, not precede it.

<a name="principles"></a>

Engineering Principles Applied Throughout

These were not written down in advance as a manifesto — they emerged, one at a time, from specific incidents in this project's own history, and are recorded here in the order they were learned.

Prove it before you fix it, and prove it again after. Every correctness claim in this document is backed by a script or a test run whose output is quoted, not merely asserted. Phase 1's proof script showed the bug; the same script, re-run after Phase 2, showed the fix.

A number that cannot be trusted is not reported as if it can be. The discarded 2,000-user run (Phase 4) and the CPU-saturated token-stress run (Finding #3) are both examples of load-test results that were explicitly excluded or caveated rather than quietly rounded into a headline claim.

Confirm which specific object a test actually touched before concluding something is broken. Learned directly from the wrong initial diagnosis during Bug #2, where the wrong product's stock was checked before the real cause was found.

Trust but verify — always clone fresh, never rely on a self-report. Learned directly from the one occasion (Finding #3) where reported work turned out not to have actually been pushed. Every subsequent piece of work in this project, including the Phase 5.5 documentation update, was independently verified against a freshly cloned copy of the repository before being considered confirmed.

Anticipate a new failure mode before building into it, when the pattern is visible in advance. Learned by contrast: most early bugs (connection pools, the verification race) were found only after the fact by a load test. Phase 5A's leader-election design broke that pattern deliberately — the "three replicas will triple the admission rate" problem was identified and designed around before any multi-instance code was written.

Separate "did my change cause this" from "is something else going on" before reacting. Learned during Phase 5.5, when a documentation-only change appeared to break the test suite; systematic layer-by-layer triage found the real cause (a Docker Desktop port-publishing glitch) rather than reflexively second-guessing or reverting a correct edit.

A deliberate decision not to add scope is itself a documented engineering decision. Recorded explicitly in the Phase 6 planning above: choosing Fulfillment over Payment, RabbitMQ over Kafka, and choosing to defer the entire phase rather than start it, are all treated as decisions worth writing down and justifying, not silent omissions.

<a name="appendix"></a>

Appendix — Full Commit Timeline
Commit	Description
498da53	Initial commit
eef2cb1	Phase 1: naive checkout API with intentional race condition
10c98e2	Phase 2: race-condition fix, row locking, idempotency keys, reservation expiry
0f9a857	Phase 4: Prometheus/Grafana observability and Locust load test with real results
5197071 / 921f5b0	README rewrite: architecture, phases, recorded load-test results, limitations
a512729	Bug #1 fix: Redis/Postgres connection pool exhaustion, graceful 503 handling
e20d769	Bug #2 fix: post-commit verification race condition
b71dd03	Repository restructured into python-service/ and java-service/
3612c20	Finding #3: admission-rate × TTL token backlog stress test
c50b945	Finding #3 follow-up: SSE + Redis pub/sub replacing status polling
88de2e0	Architecture diagram: fixed overlapping edge labels
a36e2a0	Phase 5A: three replicas, Redis leader election, Caddy X-Forwarded-For fix
6090cfb	Phase 5B: chaos testing across four failure scenarios
da6ee3c	Phase 5C: verified and documented failure modes
b4ef57c	Phase 5.5: architecture diagram and docs updated for the Phase 5A topology

This document will be extended as Phase 6, the two-language comparison, and any future work are actually built — not written in advance of the work it describes.
