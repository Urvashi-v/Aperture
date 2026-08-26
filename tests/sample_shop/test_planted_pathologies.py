"""Characterisation tests for the deliberately planted pathologies.

These tests do NOT detect anything. Detection is the analysis engine's job and
does not exist yet. What these tests do is pin the *preconditions* the
evaluation depends on:

  * the pathological endpoints really do issue the query shape we claim, and
  * the indexes that must be absent are still absent.

Without them, a well-meaning refactor could quietly remove an evaluation case
and the recall number in RESULTS.md would silently become a lie. Each test
failure message points at PATHOLOGIES.md.

Scale note: P3 and P4 are properties of the PostgreSQL planner at realistic row
counts. The test database is seeded with the `tiny` profile, where a sequential
scan over a few hundred rows is the *correct* plan, so these tests assert the
schema precondition (the index is missing) rather than a plan shape. The plan
shape is measured by the evaluation harness against a medium/large dataset.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from shop.db import get_session_factory

PATHOLOGIES = "see PATHOLOGIES.md"


async def _indexes_on(table: str) -> set[str]:
    async with get_session_factory()() as session:
        rows = await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=:t"),
            {"t": table},
        )
        return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# P1 - N+1 on order line items (GET /api/orders)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [3, 6, 10])
async def test_p1_order_queue_issues_one_item_query_per_order(
    client, seed_ids, queries, limit: int
) -> None:
    response = await client.get(
        "/api/orders",
        headers={"X-User-Id": str(seed_ids["user_id"])},
        params={"limit": limit},
    )
    assert response.status_code == 200
    returned = len(response.json()["items"])

    item_queries = queries.count_matching("from order_items")
    assert item_queries == returned, (
        f"P1 expects one order_items query per order (got {item_queries} for "
        f"{returned} orders); {PATHOLOGIES}\n{queries.summary()}"
    )


async def test_p1_query_count_grows_with_page_size(
    client, seed_ids, record_queries
) -> None:
    """The defining property of an N+1: cost scales with the result set.

    Uses two separate recordings rather than the `queries` fixture, because the
    point is the difference between two page sizes.
    """

    async def count_for(limit: int) -> int:
        with record_queries() as recorder:
            await client.get(
                "/api/orders",
                headers={"X-User-Id": str(seed_ids["user_id"])},
                params={"limit": limit},
            )
        return recorder.count_matching("from order_items")

    small = await count_for(2)
    large = await count_for(10)
    assert large > small, f"P1 is not scaling with page size; {PATHOLOGIES}"


async def test_order_detail_is_the_control_for_p1(client, seed_ids, queries) -> None:
    """The correct read of the same data: one query for all line items."""
    order_id = seed_ids["order_id"]
    response = await client.get(f"/api/orders/{order_id}")
    assert response.status_code == 200

    item_queries = queries.count_matching("from order_items")
    assert item_queries == 1, (
        "GET /api/orders/{id} is a control endpoint and must load line items in "
        f"one query, got {item_queries}\n{queries.summary()}"
    )


# ---------------------------------------------------------------------------
# P2 - N+1 on review authors (GET /api/products/{id})
# ---------------------------------------------------------------------------


async def test_p2_product_page_loads_review_authors_one_at_a_time(
    client, seed_ids, queries
) -> None:
    # Pick a product that actually has several reviews, otherwise there is no
    # N to be plus-one of.
    async with get_session_factory()() as session:
        product_id = (
            await session.execute(
                text(
                    "SELECT product_id FROM reviews GROUP BY product_id "
                    "ORDER BY count(*) DESC LIMIT 1"
                )
            )
        ).scalar_one()

    queries.statements.clear()
    response = await client.get(f"/api/products/{product_id}")
    assert response.status_code == 200
    reviews = response.json()["recent_reviews"]
    assert len(reviews) >= 2, "seed data has too few reviews to characterise P2"

    distinct_authors = {r["author"]["id"] for r in reviews if r["author"]}
    author_queries = queries.count_matching("from users where users.id =")

    # Bounded rather than exact. SQLAlchemy's identity map holds weak
    # references, so when two reviews share an author the second lookup is
    # served from memory only if the first User object has not been collected
    # yet. That makes the exact count nondeterministic; what is deterministic
    # is that the page issues one query per review it renders, capped by
    # whatever deduplication happens to occur.
    assert len(distinct_authors) <= author_queries <= len(reviews), (
        f"P2 expects roughly one users lookup per review rendered (got "
        f"{author_queries} for {len(reviews)} reviews by "
        f"{len(distinct_authors)} authors); {PATHOLOGIES}\n{queries.summary()}"
    )
    assert author_queries >= 5, (
        f"P2 must produce enough round trips to be a material N+1, got "
        f"{author_queries}; {PATHOLOGIES}"
    )


async def test_review_list_is_the_control_for_p2(client, seed_ids, queries) -> None:
    """selectinload collapses author loading into a single extra round trip."""
    response = await client.get(
        f"/api/products/{seed_ids['product_id']}/reviews", params={"limit": 50}
    )
    assert response.status_code == 200

    per_row_author_queries = queries.count_matching("from users where users.id =")
    assert per_row_author_queries == 0, (
        "GET /api/products/{id}/reviews is a control endpoint and must not load "
        f"authors one at a time\n{queries.summary()}"
    )


# ---------------------------------------------------------------------------
# P3 / P4 - missing indexes
# ---------------------------------------------------------------------------


async def test_p3_orders_user_id_is_still_unindexed(app) -> None:
    indexes = await _indexes_on("orders")
    offending = [d for d in indexes if "user_id" in d]
    assert not offending, (
        "orders.user_id has acquired an index, which removes planted pathology "
        f"P3 from the evaluation: {offending}; {PATHOLOGIES}"
    )
    # The index that *should* be there is, so the endpoint is otherwise healthy.
    assert any("placed_at" in d for d in indexes)


async def test_p4_posts_has_no_secondary_index(app) -> None:
    indexes = await _indexes_on("posts")
    secondary = [d for d in indexes if "posts_pkey" not in d]
    assert not secondary, (
        "posts has acquired a secondary index, which removes planted pathology "
        f"P4 from the evaluation: {secondary}; {PATHOLOGIES}"
    )


async def test_control_tables_keep_their_indexes(app) -> None:
    """The healthy paths must stay healthy, or the control group is worthless."""
    order_items = await _indexes_on("order_items")
    assert any("order_id" in d for d in order_items), (
        "idx_order_items_order is missing; P1 would stop being purely an N+1 "
        f"and would confound itself with a missing index; {PATHOLOGIES}"
    )

    reviews = await _indexes_on("reviews")
    assert any("product_id" in d and "created_at" in d for d in reviews), (
        f"idx_reviews_product_created is missing; {PATHOLOGIES}"
    )

    products = await _indexes_on("products")
    assert any("gin_trgm_ops" in d for d in products)


# ---------------------------------------------------------------------------
# P7 - unbounded export
# ---------------------------------------------------------------------------


async def test_p7_export_query_has_no_limit(client, seed_ids, queries) -> None:
    await client.get(
        "/api/admin/export", headers={"X-User-Id": str(seed_ids["user_id"])}
    )
    export_queries = [q for q in queries.matching("from orders") if "count(" not in q.lower()]
    assert export_queries, f"no export query recorded\n{queries.summary()}"
    assert not any("limit" in q.lower() for q in export_queries), (
        "the export query has acquired a LIMIT, which removes planted pathology "
        f"P7; {PATHOLOGIES}\n{queries.summary()}"
    )
