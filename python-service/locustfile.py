"""Locust journeys: stock-exhaust (default) and admission-token backlog stress.

Firing raw POST /checkout (especially with the test-bypass header) only
stresses the database write. Real buyers never skip the line: they take a
ticket, refresh status about once a second, then submit an admission token.

Default mode (LOADTEST_MODE unset or stock_exhaust) matches the original
500-user write-up: stock 500, checkout as soon as admitted.

token_stress mode is the admission-rate * TTL worst case: many more buyers
than stock, so the drip-feed keeps issuing tokens for minutes, and admitted
buyers wait a random time (some near the 120s TTL) before checkout. Live
token count is sampled from GET /metrics (flashbuy_admission_tokens_outstanding).
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import uuid

import gevent
import requests
from locust import HttpUser, between, events, task

# stock_exhaust: original journey. token_stress: poll for status.
# token_stress_sse: same backlog shape, wait on GET /waiting-room/stream.
LOADTEST_MODE = os.getenv("LOADTEST_MODE", "stock_exhaust")
TOKEN_STRESS = LOADTEST_MODE in ("token_stress", "token_stress_sse")
USE_SSE = LOADTEST_MODE == "token_stress_sse"

STOCK = 50 if TOKEN_STRESS else 500
ADMISSION_TIMEOUT_SECONDS = 900 if TOKEN_STRESS else 180
PRODUCT_ID = os.getenv("LOADTEST_PRODUCT_ID")

_TOKEN_GAUGE = re.compile(
    r"^flashbuy_admission_tokens_outstanding(?:\{[^}]*\})?\s+(\d+(?:\.\d+)?)\s*$",
    re.M,
)
_SSE_GAUGE = re.compile(
    r"^flashbuy_sse_connections_open(?:\{[^}]*\})?\s+(\d+(?:\.\d+)?)\s*$",
    re.M,
)

_peak_tokens = 0
_peak_sse = 0
_http_500 = 0
_http_503 = 0
_stop_sampler = False


@events.request.add_listener
def _count_capacity_statuses(response=None, **kwargs):
    """500 = unhandled crash. 503 = pool/timeout mapped on purpose."""
    global _http_500, _http_503
    if response is None:
        return
    code = getattr(response, "status_code", None)
    if code == 500:
        _http_500 += 1
    elif code == 503:
        _http_503 += 1


def _sample_outstanding_tokens(host: str) -> None:
    global _peak_tokens, _peak_sse, _stop_sampler
    while not _stop_sampler:
        try:
            text = requests.get(f"{host}/metrics", timeout=3).text
            match = _TOKEN_GAUGE.search(text)
            if match:
                value = int(float(match.group(1)))
                if value > _peak_tokens:
                    _peak_tokens = value
            sse_match = _SSE_GAUGE.search(text)
            if sse_match:
                sse_value = int(float(sse_match.group(1)))
                if sse_value > _peak_sse:
                    _peak_sse = sse_value
        except Exception:
            pass
        gevent.sleep(1.0)


@events.test_start.add_listener
def create_load_test_product(environment, **kwargs):
    """Fresh SKU so leftover Phase 1/2 orders cannot skew the run."""
    global PRODUCT_ID, _peak_tokens, _peak_sse, _http_500, _http_503, _stop_sampler
    _peak_tokens = 0
    _peak_sse = 0
    _http_500 = 0
    _http_503 = 0
    _stop_sampler = False
    host = environment.host.rstrip("/")
    name = "Locust Token Stress SKU"
    if LOADTEST_MODE == "token_stress_sse":
        name = "Locust Token Stress SSE SKU"
    elif not TOKEN_STRESS:
        name = "Locust Flash SKU"
    response = None
    last_error = None
    for _attempt in range(30):
        try:
            response = requests.post(
                f"{host}/products",
                json={"name": name, "stock": STOCK, "price_cents": 1999},
                timeout=10,
            )
            if response.status_code == 201:
                break
        except Exception as exc:
            last_error = exc
            gevent.sleep(1)
            continue
        gevent.sleep(1)
    if response is None or response.status_code != 201:
        raise RuntimeError(f"could not create load-test product: {last_error or response}")
    response.raise_for_status()
    PRODUCT_ID = response.json()["id"]
    environment.product_id = PRODUCT_ID
    print(
        f"LOADTEST mode={LOADTEST_MODE} product_id={PRODUCT_ID} stock={STOCK} "
        f"token_ttl=120s admission=20/s theoretical_ceiling=2400"
    )
    print(
        "LOADTEST note: this is a NEW row, not the seed SKU "
        "00000000-0000-4000-8000-000000000001 (that one stays at 500 unless you buy it)"
    )
    gevent.spawn(_sample_outstanding_tokens, host)


@events.test_stop.add_listener
def report_final_stock(environment, **kwargs):
    """Print Postgres stock, peak live tokens, and 500 vs 503 counts."""
    global _stop_sampler
    _stop_sampler = True
    product_id = getattr(environment, "product_id", PRODUCT_ID)
    if not product_id:
        return
    host = environment.host.rstrip("/")
    response = requests.get(f"{host}/products/{product_id}", timeout=10)
    print(f"LOADTEST final stock={response.json().get('stock')} product_id={product_id}")
    seed_id = "00000000-0000-4000-8000-000000000001"
    seed = requests.get(f"{host}/products/{seed_id}", timeout=10)
    if seed.status_code == 200:
        print(
            f"LOADTEST seed product stock={seed.json().get('stock')} "
            f"id={seed_id} (unchanged unless Locust bought this id)"
        )
    print(
        f"LOADTEST peak_live_admission_tokens={_peak_tokens} "
        f"(ceiling 20*120=2400; original 500-user run stayed ~20-40)"
    )
    print(f"LOADTEST http_500={_http_500} http_503={_http_503}")
    print(f"LOADTEST peak_open_sse_connections={_peak_sse}")
    if TOKEN_STRESS and _peak_tokens < 200:
        print(
            "LOADTEST WARNING: peak token count did not leave the original ~20-40 "
            "band; this run cannot support a pool-sizing claim for the TTL worst case."
        )


def _checkout_delay_seconds() -> float:
    """Most holders wait near TTL so tokens accumulate; a minority checkout fast."""
    if random.random() < 0.2:
        return random.uniform(0.0, 8.0)
    return random.uniform(85.0, 115.0)


class FlashBuyer(HttpUser):
    """One human: join, poll, checkout (immediately, or after a hold in token_stress)."""

    wait_time = between(0.9, 1.1)

    def on_start(self):
        """Give each user a unique identity and a unique IP so the IP bucket stays fair."""
        self.buyer_id = str(uuid.uuid4())
        self.forwarded_ip = (
            f"10.{(hash(self.buyer_id) >> 16) & 255}."
            f"{(hash(self.buyer_id) >> 8) & 255}."
            f"{(hash(self.buyer_id) & 254) + 1}"
        )
        self.headers = {"X-Forwarded-For": self.forwarded_ip}
        self.ticket_id = None
        self.admission_token = None
        self.phase = "join"
        self.checkout_at = 0.0
        self.join_deadline = time.time() + ADMISSION_TIMEOUT_SECONDS
        while not PRODUCT_ID:
            time.sleep(0.05)

    @task
    def sale_journey(self):
        """Drive join → poll → optional hold → checkout; then idle so user count stays put."""
        if self.phase == "idle":
            return
        if self.phase == "join":
            self._join()
            return
        if self.phase == "poll":
            if USE_SSE:
                self._wait_sse()
            else:
                self._poll()
            return
        if self.phase == "hold":
            if time.time() < self.checkout_at:
                return
            self._checkout()

    def _join(self):
        """Take a number. A 429 here is the rate limiter, not a checkout failure."""
        response = self.client.post(
            "/waiting-room/join",
            json={"product_id": PRODUCT_ID, "buyer_id": self.buyer_id},
            headers=self.headers,
            name="/waiting-room/join",
        )
        if response.status_code == 200:
            self.ticket_id = response.json()["ticket_id"]
            self.phase = "poll"
            self.join_deadline = time.time() + ADMISSION_TIMEOUT_SECONDS
            return
        if response.status_code == 429:
            self.phase = "idle"

    def _poll(self):
        """Browser-like ~1s poll until admitted. Hold uses no HTTP so locust is not the bottleneck."""
        if time.time() > self.join_deadline:
            self.phase = "idle"
            return
        response = self.client.get(
            f"/waiting-room/status/{self.ticket_id}",
            name="/waiting-room/status",
        )
        if response.status_code != 200:
            return
        body = response.json()
        if not body.get("admitted") or not body.get("admission_token"):
            return
        self.admission_token = body["admission_token"]
        if TOKEN_STRESS:
            self.checkout_at = time.time() + _checkout_delay_seconds()
            self.phase = "hold"
            return
        self._checkout()

    def _wait_sse(self):
        """Block this greenlet on GET /waiting-room/stream until admission.

        Locust's default HttpUser client is request/response. SSE is a held-open
        body, so this uses stream=True (same requests session Locust already has)
        and parses `data:` lines. One long request replaces ~1Hz status polls.
        """
        if time.time() > self.join_deadline:
            self.phase = "idle"
            return
        remaining = max(1.0, self.join_deadline - time.time())
        with self.client.get(
            f"/waiting-room/stream/{self.ticket_id}",
            name="/waiting-room/stream",
            headers={**self.headers, "Accept": "text/event-stream"},
            stream=True,
            timeout=remaining,
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"sse status {response.status_code}")
                if response.status_code == 503:
                    return
                self.phase = "idle"
                return
            token = None
            try:
                for raw in response.iter_lines(decode_unicode=True):
                    if time.time() > self.join_deadline:
                        break
                    if not raw:
                        continue
                    if raw.startswith("data:"):
                        payload = json.loads(raw[5:].strip())
                        if payload.get("admitted") and payload.get("admission_token"):
                            token = payload["admission_token"]
                            break
            except Exception as exc:
                response.failure(str(exc))
                self.phase = "idle"
                return
            if not token:
                response.failure("sse closed without admission")
                self.phase = "idle"
                return
            response.success()
        self.admission_token = token
        if TOKEN_STRESS:
            self.checkout_at = time.time() + _checkout_delay_seconds()
            self.phase = "hold"
            return
        self._checkout()

    def _checkout(self):
        """Never use the test-bypass header. 201 or 409 both end this buyer."""
        checkout = self.client.post(
            "/checkout",
            json={
                "product_id": PRODUCT_ID,
                "buyer_id": self.buyer_id,
                "idempotency_key": str(uuid.uuid4()),
                "admission_token": self.admission_token,
            },
            headers=self.headers,
            name="/checkout",
        )
        if checkout.status_code in (201, 409, 403):
            self.phase = "idle"
        elif TOKEN_STRESS and checkout.status_code == 503:
            # Brief retry window while still inside TTL; then give up.
            if time.time() < self.checkout_at + 30:
                self.checkout_at = time.time() + random.uniform(1.0, 3.0)
                self.phase = "hold"
            else:
                self.phase = "idle"
