"""Bulk seed loader.

Uses raw asyncpg and PostgreSQL COPY rather than the ORM. Inserting a million
rows through SQLAlchemy would take long enough that nobody would run the large
profile, and the seeder is not the thing under test - the application is. COPY
is roughly two orders of magnitude faster and is the tool PostgreSQL provides
for exactly this job.

The loader is destructive by design: it truncates before it writes, so a seed
run always produces the dataset the profile describes rather than that dataset
plus whatever was there before.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

import asyncpg

from shop.seed import generators as gen
from shop.seed.profiles import SeedProfile

logger = logging.getLogger("shop.seed")

T = TypeVar("T")

# Truncation order does not actually matter with CASCADE, but listing children
# first keeps the intent readable.
ALL_TABLES: tuple[str, ...] = (
    "follows",
    "posts",
    "reviews",
    "order_items",
    "orders",
    "products",
    "users",
    "categories",
)

# Tables with a surrogate `id` backed by a sequence. `follows` has a composite
# natural key and therefore no sequence to reset.
SEQUENCE_TABLES: tuple[str, ...] = (
    "categories",
    "users",
    "products",
    "orders",
    "order_items",
    "reviews",
    "posts",
)

PROGRESS_EVERY = 100_000


@dataclass
class SeedResult:
    profile: str
    seed: int
    duration_s: float
    counts: dict[str, int]


class SchemaMissingError(RuntimeError):
    """Raised when the seeder is pointed at a database that has no schema."""


def _counting(rows: Iterable[T], label: str) -> Iterator[T]:
    """Pass rows through, logging progress on long loads."""
    count = 0
    for row in rows:
        count += 1
        if count % PROGRESS_EVERY == 0:
            logger.info("seeding progress", extra={"table": label, "rows": count})
        yield row
    logger.info("table loaded", extra={"table": label, "rows": count})


async def _assert_schema_present(conn: asyncpg.Connection) -> None:
    found = await conn.fetchval(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = ANY($1::text[])
        """,
        list(ALL_TABLES),
    )
    if int(found) != len(ALL_TABLES):
        raise SchemaMissingError(
            "The database does not have the expected tables. "
            "Run the migrations first:  alembic upgrade head"
        )


async def _reset_sequences(conn: asyncpg.Connection) -> None:
    for table in SEQUENCE_TABLES:
        # Table names come from the constant above, never from user input.
        await conn.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                GREATEST(COALESCE((SELECT max(id) FROM {table}), 0), 1),
                COALESCE((SELECT max(id) FROM {table}), 0) > 0
            )
            """
        )
    logger.info("sequences reset")


async def _recompute_rollups(conn: asyncpg.Connection) -> None:
    """Make the denormalised columns agree with the rows that exist.

    Both rollups are computed by the database in a single statement each,
    rather than being accumulated in Python while generating. That way the
    invariant is enforced by the same query a real application would use to
    repair it, and a bug in the generator cannot produce a dataset whose
    rollups quietly disagree with its rows.
    """
    await conn.execute(
        """
        UPDATE products p
        SET rating_sum = agg.rating_sum,
            rating_count = agg.rating_count
        FROM (
            SELECT product_id,
                   sum(rating)::int AS rating_sum,
                   count(*)::int    AS rating_count
            FROM reviews
            GROUP BY product_id
        ) AS agg
        WHERE p.id = agg.product_id
        """
    )
    await conn.execute(
        """
        UPDATE orders o
        SET total_cents = agg.total_cents
        FROM (
            SELECT order_id,
                   sum(quantity * unit_price_cents)::int AS total_cents
            FROM order_items
            GROUP BY order_id
        ) AS agg
        WHERE o.id = agg.order_id
        """
    )
    logger.info("rollups recomputed")


async def count_rows(conn: asyncpg.Connection) -> dict[str, int]:
    """Exact row counts. Slow on large tables, which is fine for a report."""
    counts: dict[str, int] = {}
    for table in ALL_TABLES:
        counts[table] = int(await conn.fetchval(f"SELECT count(*) FROM {table}"))
    return counts


async def seed_database(
    dsn: str,
    profile: SeedProfile,
    random_seed: int,
    *,
    analyze: bool = True,
) -> SeedResult:
    """Truncate and reload the whole dataset. Returns real, measured counts."""
    rng = random.Random(random_seed)
    now = gen.utc_now()
    started = time.perf_counter()

    conn: asyncpg.Connection = await asyncpg.connect(dsn)
    try:
        await _assert_schema_present(conn)

        logger.info(
            "seed starting",
            extra={
                "profile": profile.name,
                "random_seed": random_seed,
                "estimated_rows": profile.approx_total_rows(),
            },
        )

        await conn.execute(
            f"TRUNCATE TABLE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE"
        )
        logger.info("existing data truncated")

        seller_count = max(1, int(profile.users * profile.seller_fraction))

        # -- categories ----------------------------------------------------
        await conn.copy_records_to_table(
            "categories",
            records=_counting(gen.gen_categories(), "categories"),
            columns=["id", "slug", "name"],
        )

        # -- users ---------------------------------------------------------
        await conn.copy_records_to_table(
            "users",
            records=_counting(
                gen.gen_users(
                    rng, profile.users, profile.seller_fraction, now, profile.history_days
                ),
                "users",
            ),
            columns=[
                "id", "email", "display_name", "is_seller", "country_code", "created_at",
            ],
        )

        # -- products ------------------------------------------------------
        # Prices are captured on the way past so order lines can use the real
        # catalogue price without reading it back out of the database.
        price_lookup: list[int] = []

        def products_with_price_capture() -> Iterator[tuple[Any, ...]]:
            for row in gen.gen_products(
                rng, profile.products, seller_count, now, profile.history_days
            ):
                price_lookup.append(row[6])  # price_cents
                yield row

        await conn.copy_records_to_table(
            "products",
            records=_counting(products_with_price_capture(), "products"),
            columns=[
                "id", "seller_id", "category_id", "sku", "title", "description",
                "price_cents", "currency", "stock_qty", "is_active",
                "rating_sum", "rating_count", "created_at",
            ],
        )

        # -- orders --------------------------------------------------------
        await conn.copy_records_to_table(
            "orders",
            records=_counting(
                gen.gen_orders(
                    rng, profile.orders, profile.users, now, profile.history_days
                ),
                "orders",
            ),
            columns=[
                "id", "user_id", "status", "total_cents", "currency",
                "shipping_country", "placed_at",
            ],
        )

        # -- order_items ---------------------------------------------------
        await conn.copy_records_to_table(
            "order_items",
            records=_counting(
                gen.gen_order_items(
                    rng,
                    profile.orders,
                    profile.products,
                    profile.max_items_per_order,
                    price_lookup,
                ),
                "order_items",
            ),
            columns=["id", "order_id", "product_id", "quantity", "unit_price_cents"],
        )

        # -- reviews -------------------------------------------------------
        await conn.copy_records_to_table(
            "reviews",
            records=_counting(
                gen.gen_reviews(
                    rng,
                    profile.reviews,
                    profile.products,
                    profile.users,
                    now,
                    profile.history_days,
                ),
                "reviews",
            ),
            columns=[
                "id", "product_id", "author_id", "rating", "title", "body", "created_at",
            ],
        )

        # -- posts ---------------------------------------------------------
        await conn.copy_records_to_table(
            "posts",
            records=_counting(
                gen.gen_posts(
                    rng,
                    profile.posts,
                    profile.users,
                    seller_count,
                    profile.products,
                    now,
                    profile.history_days,
                ),
                "posts",
            ),
            columns=[
                "id", "author_id", "product_id", "title", "body",
                "is_published", "like_count", "created_at",
            ],
        )

        # -- follows -------------------------------------------------------
        await conn.copy_records_to_table(
            "follows",
            records=_counting(
                gen.gen_follows(
                    rng,
                    profile.users,
                    seller_count,
                    profile.follows_per_user,
                    now,
                    profile.history_days,
                ),
                "follows",
            ),
            columns=["follower_id", "followed_id", "created_at"],
        )

        await _recompute_rollups(conn)
        await _reset_sequences(conn)

        if analyze:
            # Without fresh statistics the planner works from defaults and the
            # index pathologies do not reproduce. This is not optional.
            logger.info("running ANALYZE")
            await conn.execute("ANALYZE")

        counts = await count_rows(conn)
    finally:
        await conn.close()

    duration = time.perf_counter() - started
    logger.info(
        "seed complete",
        extra={"profile": profile.name, "duration_s": round(duration, 2), **counts},
    )
    return SeedResult(
        profile=profile.name,
        seed=random_seed,
        duration_s=duration,
        counts=counts,
    )


async def report_counts(dsn: str) -> dict[str, int]:
    conn: asyncpg.Connection = await asyncpg.connect(dsn)
    try:
        await _assert_schema_present(conn)
        return await count_rows(conn)
    finally:
        await conn.close()
