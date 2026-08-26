"""
Prove that naive checkout oversells under concurrency — not a generic stress test.

A stress test asks "can the server stay up under load?" This script asks a
different question: "does the stock invariant survive simultaneous buyers?"

It creates a product with exactly 10 units, then sends 50 checkout requests
at the same time (different buyer_ids). If the app is correct, at most 10 of
those requests can succeed and remaining stock cannot go negative. If more
than 10 succeed, or stock is below zero, we did not merely "go slower" — we
sold units that never existed. That is a real inventory bug, the classic
read-then-write race: two transactions both see stock > 0, both insert an
order, both decrement, and the second write is based on a stale count.

Each request uses its own idempotency_key so a later, safe checkout still
treats these as 50 distinct purchase attempts (retries are a separate test).

This script sends X-FlashBuy-Test-Bypass so it measures inventory locking,
not waiting-room drip-feed. That header is internal-only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

import httpx

DEFAULT_BASE_URL = os.getenv("FLASHBUY_BASE_URL", "http://localhost:8000")
STOCK = 10
CONCURRENCY = 50


async def create_product(client: httpx.AsyncClient, stock: int) -> dict:
    """Create a disposable product so each run starts from a known stock count."""
    response = await client.post(
        "/products",
        json={"name": "Race Probe Widget", "stock": stock, "price_cents": 100},
    )
    response.raise_for_status()
    return response.json()


async def checkout_one(
    client: httpx.AsyncClient, product_id: str, buyer_id: str
) -> httpx.Response:
    """Fire a single checkout. Failures are data, not exceptions we swallow later."""
    return await client.post(
        "/checkout",
        json={
            "product_id": product_id,
            "buyer_id": buyer_id,
            "idempotency_key": str(uuid.uuid4()),
        },
        headers={"X-FlashBuy-Test-Bypass": "1"},
    )


async def fetch_product(client: httpx.AsyncClient, product_id: str) -> dict:
    """Read stock after the burst so we can compare sold vs remaining."""
    response = await client.get(f"/products/{product_id}")
    response.raise_for_status()
    return response.json()


async def run(base_url: str) -> None:
    """Create stock=10, gather 50 concurrent checkouts, print oversell evidence."""
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        product = await create_product(client, STOCK)
        product_id = product["id"]
        print(f"Created product {product_id} with stock={STOCK}")
        print(f"Firing {CONCURRENCY} concurrent checkouts against {base_url} ...")

        tasks = [
            checkout_one(client, product_id, f"buyer-{i}")
            for i in range(CONCURRENCY)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        successes = 0
        conflicts = 0
        errors = 0
        for item in responses:
            if isinstance(item, Exception):
                errors += 1
                continue
            if item.status_code in (200, 201):
                successes += 1
            elif item.status_code == 409:
                conflicts += 1
            else:
                errors += 1

        final = await fetch_product(client, product_id)
        final_stock = final["stock"]
        oversold_by_successes = max(0, successes - STOCK)
        oversold_by_stock = max(0, -final_stock)
        oversold_by = max(oversold_by_successes, oversold_by_stock)

        print(f"Successful checkouts: {successes}")
        print(f"Conflict (409) responses: {conflicts}")
        print(f"Other errors / exceptions: {errors}")
        print(f"Final stock (GET /products/{{id}}): {final_stock}")

        if successes > STOCK or final_stock < 0:
            print(f"RACE CONDITION CONFIRMED: oversold by {oversold_by} units")
        else:
            print(
                "NO OVERSELL: successful orders == remaining invariant "
                f"(successes={successes}, stock={final_stock})"
            )


def parse_args() -> argparse.Namespace:
    """Allow pointing the probe at any running FlashBuy instance."""
    parser = argparse.ArgumentParser(description="Prove or disprove checkout oversell.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return parser.parse_args()


def main() -> None:
    """Entry point so `python scripts/prove_race_condition.py` works from the repo root."""
    args = parse_args()
    asyncio.run(run(args.base_url.rstrip("/")))


if __name__ == "__main__":
    main()
