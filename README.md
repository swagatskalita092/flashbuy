# FlashBuy

This repository holds **two independent implementations** of the same flash-sale checkout problem: sell a limited stock under concurrent demand without overselling, using a waiting room, row-level locks, idempotency keys, and reservation expiry.

The implementations are meant to be compared under **identical load-test scenarios** (same journey, same stock, same Locust shape), so concurrency and capacity behavior can be judged across stacks rather than as two unrelated demos.

| Directory | Stack | Status |
| --- | --- | --- |
| [python-service/](python-service/) | Python, FastAPI, PostgreSQL, Redis | Working implementation with tests, Docker Compose, Locust, and write-up |
| [java-service/](java-service/) | Java | In progress |

Start with **[python-service/README.md](python-service/README.md)** for architecture, how to run it, and recorded results. Run Compose and pytest from inside `python-service/` so paths in `docker-compose.yml` and the Dockerfile stay relative to that service.

`java-service` is a placeholder until the Java implementation lands.
