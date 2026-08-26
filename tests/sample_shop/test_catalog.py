"""Catalogue: categories, browse, product detail, reviews."""

from __future__ import annotations

import pytest


async def test_categories_are_returned(client) -> None:
    response = await client.get("/api/categories")
    assert response.status_code == 200
    categories = response.json()
    assert len(categories) == 12
    assert {c["slug"] for c in categories} >= {"apparel", "audio", "furniture"}


async def test_browse_is_paginated_and_bounded(client) -> None:
    response = await client.get("/api/products", params={"limit": 5})
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) <= 5
    assert page["limit"] == 5
    assert page["offset"] == 0
    # Total is opt-in, because the COUNT is a second scan.
    assert page["total"] is None


async def test_browse_total_is_opt_in(client) -> None:
    response = await client.get(
        "/api/products", params={"limit": 1, "include_total": True}
    )
    assert response.json()["total"] >= 1


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_browse_rejects_out_of_range_limits(client, limit: int) -> None:
    response = await client.get("/api/products", params={"limit": limit})
    assert response.status_code == 422


async def test_browse_filters_by_category(client, seed_ids) -> None:
    category_id = seed_ids["category_id"]
    response = await client.get(
        "/api/products", params={"category_id": category_id, "limit": 20}
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert items, "seed produced no products in the first category"

    detail_ids = [item["id"] for item in items]
    for product_id in detail_ids[:3]:
        detail = await client.get(f"/api/products/{product_id}")
        assert detail.json()["category"]["id"] == category_id


async def test_browse_price_filter(client) -> None:
    response = await client.get(
        "/api/products", params={"min_price_cents": 5000, "limit": 20}
    )
    assert response.status_code == 200
    assert all(item["price_cents"] >= 5000 for item in response.json()["items"])


async def test_browse_only_returns_active_products(client) -> None:
    response = await client.get("/api/products", params={"limit": 100})
    assert all(item["is_active"] for item in response.json()["items"])


async def test_product_detail_shape(client, seed_ids) -> None:
    response = await client.get(f"/api/products/{seed_ids['product_id']}")
    assert response.status_code == 200
    product = response.json()
    assert product["id"] == seed_ids["product_id"]
    assert product["category"]["slug"]
    assert product["seller"]["id"] > 0
    assert isinstance(product["recent_reviews"], list)


async def test_product_detail_404(client) -> None:
    response = await client.get("/api/products/99999999")
    assert response.status_code == 404
    assert response.json()["error"]["status"] == 404


async def test_average_rating_matches_the_rollup(client, seed_ids) -> None:
    """The denormalised rating rollup has to agree with the reviews table."""
    product_id = seed_ids["product_id"]
    detail = (await client.get(f"/api/products/{product_id}")).json()

    all_reviews: list[dict] = []
    offset = 0
    while True:
        page = (
            await client.get(
                f"/api/products/{product_id}/reviews",
                params={"limit": 100, "offset": offset},
            )
        ).json()
        all_reviews.extend(page["items"])
        if len(page["items"]) < 100:
            break
        offset += 100

    assert detail["rating_count"] == len(all_reviews)
    if all_reviews:
        expected = round(sum(r["rating"] for r in all_reviews) / len(all_reviews), 2)
        assert detail["average_rating"] == pytest.approx(expected, abs=0.01)
    else:
        assert detail["average_rating"] is None


async def test_review_list_includes_authors(client, seed_ids) -> None:
    response = await client.get(
        f"/api/products/{seed_ids['product_id']}/reviews", params={"limit": 5}
    )
    assert response.status_code == 200
    for review in response.json()["items"]:
        assert review["author"]["display_name"]


async def test_create_review_requires_authentication(client, seed_ids) -> None:
    response = await client.post(
        f"/api/products/{seed_ids['product_id']}/reviews",
        json={"rating": 5, "title": "t", "body": "b"},
    )
    assert response.status_code == 401


async def test_create_review_updates_the_rollup(client, seed_ids) -> None:
    product_id = seed_ids["product_id"]
    user_id = seed_ids["user_id"]

    before = (await client.get(f"/api/products/{product_id}")).json()

    created = await client.post(
        f"/api/products/{product_id}/reviews",
        headers={"X-User-Id": str(user_id)},
        json={"rating": 5, "title": "Works", "body": "Bought two."},
    )
    assert created.status_code == 201
    assert created.json()["author"]["id"] == user_id

    after = (await client.get(f"/api/products/{product_id}")).json()
    assert after["rating_count"] == before["rating_count"] + 1


@pytest.mark.parametrize("rating", [0, 6, -3])
async def test_review_rating_is_validated(client, seed_ids, rating: int) -> None:
    response = await client.post(
        f"/api/products/{seed_ids['product_id']}/reviews",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        json={"rating": rating},
    )
    assert response.status_code == 422
