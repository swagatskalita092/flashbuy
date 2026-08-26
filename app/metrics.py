"""Prometheus metrics for a flash-sale checkout.

These are not vanity counters. Under a drop, operators need to know:

- Queue depth: if it climbs faster than admission (N per second), demand is
  outrunning the valve. Raise N only if checkout p95 stays healthy; otherwise
  the waiting room is doing its job and the database is the bottleneck.
- Stock remaining: should fall in lock-step with successful checkouts and
  never go negative. A drop that is steeper than successes means a bug (the
  Phase 1 race). A drop that stalls while the queue is empty means nobody
  is getting admitted.
- Latency histograms (not just averages): a 50ms average can hide a p99 of
  2s, which is what the last people in line actually feel. We histogram
  /checkout and /waiting-room/join because those are the two moments a buyer
  is blocked on us (status polls are cheap Redis reads).
- Success vs rejection by reason: 409 means inventory is gone (expected at
  the end of a sale). 429 means the rate limiter is eating bots or we set
  the bucket too tight for real fans. 403 means tokens expired before
  checkout — admission TTL or client slowness, not stock.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
import time

from prometheus_client import Counter, Gauge, Histogram

# Buckets in seconds. Flash-sale checkouts should sit in the low tens of ms
# when the waiting room is doing its job; 1s+ means lock queues on Postgres.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


checkout_latency_seconds = Histogram(
    "flashbuy_checkout_latency_seconds",
    "Wall time of POST /checkout. p95 is the buyer-facing 'did I get it?' delay.",
    buckets=_LATENCY_BUCKETS,
)

join_latency_seconds = Histogram(
    "flashbuy_join_latency_seconds",
    "Wall time of POST /waiting-room/join. Spikes here mean Redis or rate-limit Lua is struggling.",
    buckets=_LATENCY_BUCKETS,
)

waiting_room_queue_depth = Gauge(
    "flashbuy_waiting_room_queue_depth",
    "People still waiting (not yet admitted) for a product. Rising = admission lagging demand.",
    ["product_id"],
)

stock_remaining = Gauge(
    "flashbuy_stock_remaining",
    "Units left on the shelf. Must track successful checkouts; negative is oversell.",
    ["product_id"],
)

checkouts_succeeded_total = Counter(
    "flashbuy_checkouts_succeeded_total",
    "Reservations that took a unit. Should stop when stock hits 0, not keep climbing.",
)

checkouts_rejected_total = Counter(
    "flashbuy_checkouts_rejected_total",
    "Failed checkout or join attempts by why they failed (capacity vs abuse vs expired token).",
    ["reason"],
)


@contextmanager
def observe_latency(histogram: Histogram) -> Iterator[None]:
    """Record duration even when the handler raises (failed checkouts still have a p95)."""
    started = time.perf_counter()
    try:
        yield
    finally:
        histogram.observe(time.perf_counter() - started)


def set_queue_depth(product_id: str, depth: int) -> None:
    """Push the live Redis ZCARD into Prometheus so Grafana is not one request behind."""
    waiting_room_queue_depth.labels(product_id=product_id).set(depth)


def set_stock(product_id: str, remaining: int) -> None:
    """Push Postgres stock after every mutation; scrapes would be stale between intervals."""
    stock_remaining.labels(product_id=str(product_id)).set(remaining)


def record_success() -> None:
    """One more unit sold. Combined with stock gauge this is the oversell detector."""
    checkouts_succeeded_total.inc()


def record_rejection(reason: str) -> None:
    """reason is a short enum-like label so Grafana can stack them without parsing log text."""
    checkouts_rejected_total.labels(reason=reason).inc()
