"""User profiles, order history, the fulfilment queue, ordering and checkout."""

from __future__ import annotations

import pytest


async def test_user_profile(client, seed_ids) -> None:
    response = await client.get(f"/api/users/{seed_ids['user_id']}")
    assert response.status_code == 200
    user = response.json()
    assert user["id"] == seed_ids["user_id"]
    assert "@" in user["email"]


async def test_user_profile_404(client) -> None:
    assert (await client.get("/api/users/99999999")).status_code == 404


async def test_order_history_totals_are_consistent(client, seed_ids) -> None:
    buyer_id = seed_ids["buyer_id"]
    response = await client.get(
        f"/api/users/{buyer_id}/orders", params={"limit": 100}
    )
    assert response.status_code == 200
    page = response.json()
    assert page["total"] >= len(page["items"])

    for summary in page["items"]:
        detail = (await client.get(f"/api/orders/{summary['id']}")).json()
        assert detail["user_id"] == buyer_id
        assert summary["item_count"] == len(detail["items"])
        computed = sum(i["quantity"] * i["unit_price_cents"] for i in detail["items"])
        assert summary["total_cents"] == computed


async def test_order_history_pagination_does_not_repeat_rows(client, seed_ids) -> None:
    buyer_id = seed_ids["buyer_id"]
    first = (
        await client.get(f"/api/users/{buyer_id}/orders", params={"limit": 2})
    ).json()
    second = (
        await client.get(
            f"/api/users/{buyer_id}/orders", params={"limit": 2, "offset": 2}
        )
    ).json()
    first_ids = {o["id"] for o in first["items"]}
    second_ids = {o["id"] for o in second["items"]}
    assert not (first_ids & second_ids)


async def test_user_reviews_are_all_authored_by_that_user(client, seed_ids) -> None:
    user_id = seed_ids["user_id"]
    response = await client.get(f"/api/users/{user_id}/reviews", params={"limit": 50})
    assert response.status_code == 200
    for review in response.json()["items"]:
        assert review["author"]["id"] == user_id


async def test_fulfilment_queue_requires_authentication(client) -> None:
    assert (await client.get("/api/orders")).status_code == 401


async def test_fulfilment_queue_is_newest_first(client, seed_ids) -> None:
    response = await client.get(
        "/api/orders",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        params={"limit": 10},
    )
    assert response.status_code == 200
    placed = [o["placed_at"] for o in response.json()["items"]]
    assert placed == sorted(placed, reverse=True)


async def test_fulfilment_queue_rejects_unknown_status(client, seed_ids) -> None:
    response = await client.get(
        "/api/orders",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        params={"status": "teleported"},
    )
    assert response.status_code == 422


async def test_fulfilment_queue_status_filter(client, seed_ids) -> None:
    response = await client.get(
        "/api/orders",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        params={"status": "delivered", "limit": 20},
    )
    assert response.status_code == 200
    assert all(o["status"] == "delivered" for o in response.json()["items"])


async def test_fulfilment_queue_matches_order_detail(client, seed_ids) -> None:
    """The pathological listing and the correct detail read must agree.

    This is the assertion that makes P1 a *performance* bug rather than a
    correctness bug: the N+1 version returns exactly the same data, just far
    more slowly.
    """
    listing = (
        await client.get(
            "/api/orders",
            headers={"X-User-Id": str(seed_ids["user_id"])},
            params={"limit": 5},
        )
    ).json()

    for order in listing["items"]:
        detail = (await client.get(f"/api/orders/{order['id']}")).json()
        assert detail == order


async def test_order_detail_404(client) -> None:
    assert (await client.get("/api/orders/99999999")).status_code == 404


async def test_place_order_decrements_stock(client, seed_ids) -> None:
    product_id = seed_ids["product_id"]
    user_id = seed_ids["user_id"]

    before = (await client.get(f"/api/products/{product_id}")).json()["stock_qty"]

    response = await client.post(
        "/api/orders",
        headers={"X-User-Id": str(user_id)},
        json={"items": [{"product_id": product_id, "quantity": 2}]},
    )
    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "pending"
    assert order["total_cents"] == order["items"][0]["unit_price_cents"] * 2

    after = (await client.get(f"/api/products/{product_id}")).json()["stock_qty"]
    assert after == before - 2


async def test_place_order_rejects_unknown_product(client, seed_ids) -> None:
    response = await client.post(
        "/api/orders",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        json={"items": [{"product_id": 99999999, "quantity": 1}]},
    )
    assert response.status_code == 404


async def test_place_order_rejects_duplicate_lines(client, seed_ids) -> None:
    product_id = seed_ids["product_id"]
    response = await client.post(
        "/api/orders",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        json={
            "items": [
                {"product_id": product_id, "quantity": 1},
                {"product_id": product_id, "quantity": 1},
            ]
        },
    )
    assert response.status_code == 422


async def test_place_order_rejects_insufficient_stock(client, seed_ids) -> None:
    """Order more than exists. Needs a genuinely low-stock product to try it on.

    The API caps quantity at 100, so a product with 400 in stock cannot be
    over-ordered through the public interface at all.
    """
    from sqlalchemy import select

    from shop.db import get_session_factory
    from shop.models import Product

    async with get_session_factory()() as session:
        product = (
            await session.execute(
                select(Product)
                .where(Product.stock_qty < 50, Product.is_active.is_(True))
                .order_by(Product.stock_qty)
                .limit(1)
            )
        ).scalar_one_or_none()

    assert product is not None, "seed produced no low-stock product to test against"

    response = await client.post(
        "/api/orders",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        json={
            "items": [{"product_id": product.id, "quantity": product.stock_qty + 1}]
        },
    )
    assert response.status_code == 409
    assert "stock" in response.json()["error"]["detail"].lower()


async def test_place_order_rejects_a_delisted_product(client, seed_ids) -> None:
    from sqlalchemy import select

    from shop.db import get_session_factory
    from shop.models import Product

    async with get_session_factory()() as session:
        product = (
            await session.execute(
                select(Product).where(Product.is_active.is_(False)).limit(1)
            )
        ).scalar_one_or_none()

    assert product is not None, "seed produced no delisted product to test against"

    response = await client.post(
        "/api/orders",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        json={"items": [{"product_id": product.id, "quantity": 1}]},
    )
    assert response.status_code == 409
    assert "available" in response.json()["error"]["detail"].lower()


async def test_failed_order_does_not_write_partial_state(client, seed_ids) -> None:
    """A rejected order must leave stock and order count untouched."""
    product_id = seed_ids["product_id"]
    user_id = seed_ids["user_id"]

    stock_before = (await client.get(f"/api/products/{product_id}")).json()["stock_qty"]
    orders_before = (
        await client.get(f"/api/users/{user_id}/orders", params={"limit": 1})
    ).json()["total"]

    response = await client.post(
        "/api/orders",
        headers={"X-User-Id": str(user_id)},
        json={
            "items": [
                {"product_id": product_id, "quantity": 1},
                {"product_id": 99999999, "quantity": 1},
            ]
        },
    )
    assert response.status_code == 404

    stock_after = (await client.get(f"/api/products/{product_id}")).json()["stock_qty"]
    orders_after = (
        await client.get(f"/api/users/{user_id}/orders", params={"limit": 1})
    ).json()["total"]

    assert stock_after == stock_before
    assert orders_after == orders_before


async def test_checkout_moves_pending_to_paid(client, seed_ids) -> None:
    user_id = seed_ids["user_id"]
    order = (
        await client.post(
            "/api/orders",
            headers={"X-User-Id": str(user_id)},
            json={"items": [{"product_id": seed_ids["product_id"], "quantity": 1}]},
        )
    ).json()

    response = await client.post(
        "/api/checkout",
        headers={"X-User-Id": str(user_id)},
        json={"order_id": order["id"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "paid"


async def test_checkout_is_not_repeatable(client, seed_ids) -> None:
    user_id = seed_ids["user_id"]
    order = (
        await client.post(
            "/api/orders",
            headers={"X-User-Id": str(user_id)},
            json={"items": [{"product_id": seed_ids["product_id"], "quantity": 1}]},
        )
    ).json()
    payload = {"order_id": order["id"]}
    headers = {"X-User-Id": str(user_id)}

    assert (await client.post("/api/checkout", headers=headers, json=payload)).status_code == 200
    second = await client.post("/api/checkout", headers=headers, json=payload)
    assert second.status_code == 409


async def test_checkout_hides_other_peoples_orders(client, seed_ids) -> None:
    """Someone else's order must look absent, not forbidden."""
    owner_id = seed_ids["user_id"]
    order = (
        await client.post(
            "/api/orders",
            headers={"X-User-Id": str(owner_id)},
            json={"items": [{"product_id": seed_ids["product_id"], "quantity": 1}]},
        )
    ).json()

    other_id = seed_ids["buyer_id"] if seed_ids["buyer_id"] != owner_id else owner_id + 1
    response = await client.post(
        "/api/checkout",
        headers={"X-User-Id": str(other_id)},
        json={"order_id": order["id"]},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("user_header", ["", "not-a-number"])
async def test_bad_user_header_is_rejected(client, user_header: str) -> None:
    response = await client.get("/api/orders", headers={"X-User-Id": user_header})
    assert response.status_code in (401, 422)


async def test_unknown_user_header_is_rejected(client) -> None:
    response = await client.get("/api/orders", headers={"X-User-Id": "99999999"})
    assert response.status_code == 401
