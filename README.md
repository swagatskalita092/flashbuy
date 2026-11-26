<div align="center">

<img src="assets/banner.png" alt="FlashBuy. Flash-sale checkout and durable fulfillment. Sell limited stock under concurrent demand without overselling." width="100%">

<br>

[![tests](https://github.com/swagatskalita092/flashbuy/actions/workflows/tests.yml/badge.svg)](https://github.com/swagatskalita092/flashbuy/actions/workflows/tests.yml)
![License](https://img.shields.io/badge/license-MIT-2563EB?style=for-the-badge&labelColor=111827)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.24279725-f97316?style=for-the-badge&labelColor=111827)](https://doi.org/10.5281/zenodo.24279725)

![FastAPI](https://img.shields.io/badge/FastAPI-111827?style=flat-square&logo=fastapi&logoColor=34D8C0)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-111827?style=flat-square&logo=postgresql&logoColor=8CB4E6)
![Redis](https://img.shields.io/badge/Redis-111827?style=flat-square&logo=redis&logoColor=DC382D)
![RabbitMQ](https://img.shields.io/badge/RabbitMQ-111827?style=flat-square&logo=rabbitmq&logoColor=FF6600)
![Docker](https://img.shields.io/badge/Docker-111827?style=flat-square&logo=docker&logoColor=2496ED)
![Locust](https://img.shields.io/badge/Locust-111827?style=flat-square&logo=locust&logoColor=38B832)

[Python service README](python-service/README.md) &nbsp;|&nbsp; [Results](python-service/README.md#results) &nbsp;|&nbsp; [Failure modes](python-service/docs/failure-modes.md) &nbsp;|&nbsp; [Engineering journal](python-service/docs/ENGINEERING_JOURNAL.md)

<br>

<img src="assets/architecture-flow.gif" alt="Animated diagram of a request moving through the waiting room, leader-only admission, the Postgres checkout lock, the transactional outbox, RabbitMQ, and the independent fulfillment service's idempotent inbox." width="900">

</div>

<br>

## <img src="assets/icons/book-open.svg" width="24" height="24" align="middle">&nbsp; What this is

This repository holds **two independent implementations** of the same
flash-sale checkout problem: sell a limited number of units under heavy
concurrent demand without overselling, using a waiting room, row-level locks,
idempotency keys, and reservation expiry — then, past the sale itself, get a
confirmed order to a completely separate fulfillment service **reliably**,
using a transactional outbox instead of a shared database.

The implementations are meant to be compared under **identical load-test
scenarios** (same journey, same stock, same Locust shape), so concurrency and
capacity behavior can be judged across stacks rather than as two unrelated
demos.

| Directory | Stack | Status |
| --- | --- | --- |
| [python-service/](python-service/) | FastAPI, PostgreSQL, Redis, RabbitMQ | Working implementation: tests, Docker Compose, Locust load tests, chaos-tested outbox, and a full write-up |
| [java-service/](java-service/) | Java | Placeholder, not implemented yet |

**Start with [python-service/README.md](python-service/README.md)** — it has
the architecture, how to run it, and every recorded result. Run Compose and
pytest from inside `python-service/` so paths in `docker-compose.yml` and the
Dockerfile stay relative to that service.

## <img src="assets/icons/compass.svg" width="24" height="24" align="middle">&nbsp; Why this matters

Flash sales fail in a specific way. Thousands of people hit the same SKU in
the same second — a concert on-sale, a Black Friday drop, a console restock —
and a naive read-then-write path lets two transactions both see `stock > 0`
and both succeed, so the shop sells more units than it has. Fix that and the
database still can't absorb every concurrent checkout at once: connections,
lock waits, and query time pile up, and the site times out for everyone,
including people who would have gotten a unit. A waiting room that admits on
a drip, instead of letting everyone fight over the same row at once, is the
actual production answer — the same category of system as commercial virtual
waiting rooms (Queue-it and similar), just a small implementation of it, not
a competitor to them.

Past the sale, a second, quieter failure mode: once an order is confirmed, a
completely separate fulfillment service — its own database, no shared
transaction — has to find out, *reliably*, over a message broker. A plain
"write to the database, then publish to the queue" has a gap where the
process can crash between the two, silently losing the event. FlashBuy closes
that gap with a transactional outbox, and then actually breaks it on purpose
— killing the publisher, the consumer, and the broker mid-flight against real
containers — to find out if the design holds up, not just whether it compiles.

## <img src="assets/icons/layers.svg" width="24" height="26" align="middle">&nbsp; The system at a glance

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#1e2a44','primaryTextColor':'#f8fafc','primaryBorderColor':'#f97316','lineColor':'#3b4a6b','fontFamily':'Inter, Segoe UI, sans-serif'}}}%%
flowchart LR
    B["Buyer"] --> W["Waiting room<br/>Redis sorted set"]
    W --> AD["Admission<br/>leader-only, 20/s"]
    AD --> C["Checkout<br/>Postgres row lock"]
    C --> CF["Confirm"]
    CF --> OB[("outbox_events<br/>same commit")]
    OB --> P["Outbox publisher<br/>publisher confirms"]
    P --> MQ["RabbitMQ<br/>order.confirmed"]
    MQ --> FS["Fulfillment service<br/>separate DB"]
    FS --> IB[("inbox_events<br/>idempotent")]
    classDef core fill:#111827,stroke:#f97316,color:#f8fafc;
    class OB,IB core;
```

Everything left of the outbox is one service's transaction. Everything right
of it is a second, independent one that only ever hears about the first
through a durable, at-least-once event — never a shared table, never a
synchronous call it can block on.

## <img src="assets/icons/shield.svg" width="24" height="24" align="middle">&nbsp; Principles

| Principle | In practice |
| --- | --- |
| **Prove it, don't assert it** | Every number in the results tables comes from a committed Locust run or a hand-run chaos drill, with the evidence file linked next to the claim |
| **Admit on a drip, not a poll** | Thousands of clients asking "am I admitted yet?" every second is a self-inflicted overload; FlashBuy pushes the token over SSE instead |
| **Atomic or it doesn't count** | The outbox write and the order-confirmed status change happen in the same commit, by construction, not by convention |
| **At-least-once, made safe** | Duplicate delivery is allowed and expected; the idempotent inbox is what turns it into a non-event instead of a duplicate job |
| **Break it on purpose** | Chaos tests kill the publisher, the consumer, and the broker against real containers, not mocks, and report what actually happened — bugs included |

## <img src="assets/icons/alert.svg" width="24" height="24" align="middle">&nbsp; Verified gaps

Documenting a limitation honestly is part of the write-up, not a footnote.
Full detail: [python-service/docs/failure-modes.md](python-service/docs/failure-modes.md).

| Gap | Status |
| --- | --- |
| `outbox_events` and `inbox_events` grow without bound | **Fixed for the outbox side** — `app/outbox_retention.py`, tested against real Postgres (28/28 tests pass). `inbox_events` has the identical gap, not yet addressed. |
| The outbox publisher is safe today only because exactly one instance runs | **Not fixed** — documented as a scaling ceiling in the current design, not a bug in the system as it runs today. |

## <img src="assets/icons/scale.svg" width="24" height="24" align="middle">&nbsp; License

MIT. See [LICENSE](LICENSE). `java-service` is a placeholder until the Java
implementation lands.

## <img src="assets/icons/user.svg" width="24" height="24" align="middle">&nbsp; Author

Swagat Subhash Kalita designs, builds, and load-tests FlashBuy.

[GitHub](https://github.com/swagatskalita092) &nbsp;|&nbsp; [LinkedIn](https://linkedin.com/in/swagat-s-kalita)
