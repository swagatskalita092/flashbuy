import pytest

pytestmark = pytest.mark.asyncio


async def _create_product(client, stock: int):
    response = await client.post(
        "/products",
        json={"name": "Test Widget", "stock": stock, "price_cents": 999},
    )
    assert response.status_code == 201
    return response.json()


async def test_successful_checkout_reduces_stock_by_one(client):
    product = await _create_product(client, stock=5)

    checkout = await client.post(
        "/checkout",
        json={"product_id": product["id"], "buyer_id": "buyer-1"},
    )
    assert checkout.status_code == 201
    body = checkout.json()
    assert body["buyer_id"] == "buyer-1"
    assert body["status"] == "confirmed"
    assert body["product_id"] == product["id"]

    fetched = await client.get(f"/products/{product['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["stock"] == 4


async def test_checkout_on_zero_stock_returns_409(client):
    product = await _create_product(client, stock=0)

    checkout = await client.post(
        "/checkout",
        json={"product_id": product["id"], "buyer_id": "buyer-1"},
    )
    assert checkout.status_code == 409
    assert checkout.json()["detail"] == "out of stock"

    fetched = await client.get(f"/products/{product['id']}")
    assert fetched.json()["stock"] == 0


async def test_create_and_get_product(client):
    created = await _create_product(client, stock=10)
    fetched = await client.get(f"/products/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Test Widget"
    assert fetched.json()["stock"] == 10
    assert fetched.json()["price_cents"] == 999
