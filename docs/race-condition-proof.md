# Race condition proof: before and after Phase 2

This note records **real output** from `python scripts/prove_race_condition.py`
(50 concurrent checkouts against a **fresh product with stock = 10**).
The script is not a stress test; it checks whether we sold more units than exist.

## Before (Phase 1, unlocked read-then-write)

Ran against the naive checkout (no `SELECT FOR UPDATE`) on 2026-08-26:

```
Created product aa00a5f8-63dc-48ba-ad1d-f1e91d0f85bb with stock=10
Firing 50 concurrent checkouts against http://localhost:8000 ...
Successful checkouts: 50
Conflict (409) responses: 0
Other errors / exceptions: 0
Final stock (GET /products/{id}): 6
RACE CONDITION CONFIRMED: oversold by 40 units
```

All 50 buyers got a success while only 10 units existed. Final stock was still
positive (6) because many decrements raced on the same stale value. That is
overselling, not "the server was slow".

## After (Phase 2, `SELECT FOR UPDATE` + unique idempotency keys)

Ran against the locked checkout on 2026-08-26:

```
Created product af625009-7007-4bc8-b605-0dfcd7ba2653 with stock=10
Firing 50 concurrent checkouts against http://localhost:8000 ...
Successful checkouts: 10
Conflict (409) responses: 40
Other errors / exceptions: 0
Final stock (GET /products/{id}): 0
NO OVERSELL: successful orders == remaining invariant (successes=10, stock=0)
```

Exactly 10 reservations, 40 conflicts, stock 0. The extra 40 buyers wait on the
row lock, then see empty stock instead of selling units that do not exist.
