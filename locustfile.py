"""Locust journey: join the waiting room, poll like a browser, then checkout.

Firing raw POST /checkout (especially with the test-bypass header) only
stresses the database write. Real buyers never skip the line: they take a
ticket, refresh status about once a second, then submit an admission token.
That path is where Redis rate limits, drip-feed admission, and token TTL
show up. Skipping it can make the system look faster than any actual flash
sale would feel.
"""

from __future__ import annotations

import os
import time
import uuid

import requests
from locust import HttpUser, between, events, task

STOCK = 500
ADMISSION_TIMEOUT_SECONDS = 180
PRODUCT_ID = os.getenv("LOADTEST_PRODUCT_ID")


@events.test_start.add_listener
def create_load_test_product(environment, **kwargs):
    """Fresh SKU with stock=500 so leftover Phase 1/2 orders cannot skew the run."""
    global PRODUCT_ID
    host = environment.host.rstrip("/")
    response = requests.post(
        f"{host}/products",
        json={"name": "Locust Flash SKU", "stock": STOCK, "price_cents": 1999},
        timeout=10,
    )
    response.raise_for_status()
    PRODUCT_ID = response.json()["id"]
    environment.product_id = PRODUCT_ID
    print(f"LOADTEST product_id={PRODUCT_ID} stock={STOCK}")


@events.test_stop.add_listener
def report_final_stock(environment, **kwargs):
    """Print Postgres stock after the run so we can confirm zero oversell."""
    product_id = getattr(environment, "product_id", PRODUCT_ID)
    if not product_id:
        return
    host = environment.host.rstrip("/")
    response = requests.get(f"{host}/products/{product_id}", timeout=10)
    print(f"LOADTEST final stock={response.json().get('stock')} product_id={product_id}")


class FlashBuyer(HttpUser):
    """One human: join once, poll until admitted or timeout, checkout once, then idle."""

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
        self.join_deadline = time.time() + ADMISSION_TIMEOUT_SECONDS
        while not PRODUCT_ID:
            time.sleep(0.05)

    @task
    def sale_journey(self):
        """Drive the three real steps; idle afterwards so Locust user count stays at the target."""
        if self.phase == "idle":
            return
        if self.phase == "join":
            self._join()
            return
        if self.phase == "poll":
            self._poll_or_checkout()

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

    def _poll_or_checkout(self):
        """Browser-like 1s poll. Checkout only after a live token — never with the bypass header."""
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
        token = body["admission_token"]
        checkout = self.client.post(
            "/checkout",
            json={
                "product_id": PRODUCT_ID,
                "buyer_id": self.buyer_id,
                "idempotency_key": str(uuid.uuid4()),
                "admission_token": token,
            },
            headers=self.headers,
            name="/checkout",
        )
        # 201 = got a unit; 409 = sale honestly sold out. Either way this user is done.
        if checkout.status_code in (201, 409, 403):
            self.phase = "idle"
