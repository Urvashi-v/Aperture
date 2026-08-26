"""Community feed, post permalinks, and the back-office endpoints."""

from __future__ import annotations


async def test_feed_requires_authentication(client) -> None:
    assert (await client.get("/api/feed")).status_code == 401


async def test_feed_only_contains_followed_authors(client, seed_ids, app) -> None:
    follower_id = seed_ids["follower_id"]

    from sqlalchemy import select

    from shop.db import get_session_factory
    from shop.models import Follow

    async with get_session_factory()() as session:
        followed = set(
            (
                await session.execute(
                    select(Follow.followed_id).where(Follow.follower_id == follower_id)
                )
            )
            .scalars()
            .all()
        )

    response = await client.get(
        "/api/feed", headers={"X-User-Id": str(follower_id)}, params={"limit": 50}
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert items, "the most-following user has an empty feed; seed data is wrong"
    for post in items:
        assert post["author"]["id"] in followed


async def test_feed_excludes_drafts_and_is_newest_first(client, seed_ids) -> None:
    response = await client.get(
        "/api/feed",
        headers={"X-User-Id": str(seed_ids["follower_id"])},
        params={"limit": 50},
    )
    items = response.json()["items"]
    assert all(post["is_published"] for post in items)
    created = [post["created_at"] for post in items]
    assert created == sorted(created, reverse=True)


async def test_post_permalink(client, seed_ids) -> None:
    response = await client.get(f"/api/posts/{seed_ids['post_id']}")
    assert response.status_code == 200
    post = response.json()
    assert post["id"] == seed_ids["post_id"]
    assert post["author"]["display_name"]


async def test_post_permalink_404(client) -> None:
    assert (await client.get("/api/posts/99999999")).status_code == 404


async def test_create_post(client, seed_ids) -> None:
    response = await client.post(
        "/api/posts",
        headers={"X-User-Id": str(seed_ids["seller_id"])},
        json={
            "title": "Restock: the walnut side table is back",
            "body": "Second batch landed this morning.",
            "product_id": seed_ids["product_id"],
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["author"]["id"] == seed_ids["seller_id"]
    assert created["product_id"] == seed_ids["product_id"]

    fetched = (await client.get(f"/api/posts/{created['id']}")).json()
    assert fetched["title"] == created["title"]


async def test_create_post_rejects_unknown_product(client, seed_ids) -> None:
    response = await client.post(
        "/api/posts",
        headers={"X-User-Id": str(seed_ids["seller_id"])},
        json={"title": "Nope", "product_id": 99999999},
    )
    assert response.status_code == 404


async def test_create_post_requires_a_title(client, seed_ids) -> None:
    response = await client.post(
        "/api/posts",
        headers={"X-User-Id": str(seed_ids["seller_id"])},
        json={"title": "", "body": "no title"},
    )
    assert response.status_code == 422


async def test_admin_stats_requires_authentication(client) -> None:
    assert (await client.get("/api/admin/stats")).status_code == 401


async def test_admin_stats_are_plausible(client, seed_ids) -> None:
    """Counts come from planner estimates, so assert shape, not exact equality.

    Asserting an exact row count here would be asserting that `reltuples` is
    exact, which it is not and is not meant to be. What must hold is that the
    numbers are populated and in the right ballpark after ANALYZE.
    """
    response = await client.get(
        "/api/admin/stats", headers={"X-User-Id": str(seed_ids["user_id"])}
    )
    assert response.status_code == 200
    stats = response.json()

    assert stats["users"] > 0
    assert stats["products"] > 0
    assert stats["orders"] > 0
    assert stats["order_items"] >= stats["orders"]
    assert 0 < stats["sellers"] < stats["users"]
    assert stats["gross_merchandise_value_cents"] >= 0


async def test_export_requires_authentication(client) -> None:
    assert (await client.get("/api/admin/export")).status_code == 401


async def test_export_returns_every_order(client, seed_ids, app) -> None:
    """P7 is that this endpoint is unbounded; the test pins that behaviour.

    If someone later adds a LIMIT, this test fails and points them at
    PATHOLOGIES.md rather than letting an evaluation case disappear quietly.
    """
    from sqlalchemy import func, select

    from shop.db import get_session_factory
    from shop.models import Order

    async with get_session_factory()() as session:
        total = (
            await session.execute(select(func.count()).select_from(Order))
        ).scalar_one()

    response = await client.get(
        "/api/admin/export", headers={"X-User-Id": str(seed_ids["user_id"])}
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == total
    assert {"order_id", "user_id", "status", "total_cents", "placed_at"} <= set(rows[0])


async def test_export_respects_an_explicit_window(client, seed_ids) -> None:
    headers = {"X-User-Id": str(seed_ids["user_id"])}
    everything = (await client.get("/api/admin/export", headers=headers)).json()
    bounded = (
        await client.get(
            "/api/admin/export",
            headers=headers,
            params={"since": "2099-01-01T00:00:00Z"},
        )
    ).json()
    assert bounded == []
    assert len(everything) > 0
