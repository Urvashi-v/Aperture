"""Seed dataset sizes.

Row counts are a first-class experimental variable, not a convenience. The
PostgreSQL planner chooses a sequential scan over an index based on estimated
row counts, so a missing index on a 500-row table is invisible and a missing
index on a 5,000,000-row table is catastrophic. The planted index pathologies
(P3, P4) only reproduce at a realistic scale, which is why the profiles below
go well past what is comfortable on a laptop.

Nothing here asserts that any of these rows exist. They exist once you have
actually run the seeder at that profile, and `shop-seed --report` prints the
real counts from the database.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeedProfile:
    name: str
    description: str

    users: int
    seller_fraction: float          # share of users who list products
    products: int
    orders: int
    max_items_per_order: int
    reviews: int
    posts: int
    follows_per_user: int
    history_days: int               # spread of created_at / placed_at values

    def approx_order_items(self) -> int:
        """Expected order_items rows, for the pre-run estimate only."""
        mean_items = (1 + self.max_items_per_order) / 2
        return int(self.orders * mean_items)

    def approx_total_rows(self) -> int:
        return (
            self.users
            + self.products
            + self.orders
            + self.approx_order_items()
            + self.reviews
            + self.posts
            + self.users * self.follows_per_user
        )


PROFILES: dict[str, SeedProfile] = {
    "tiny": SeedProfile(
        name="tiny",
        description="Automated tests. Seconds to build, too small to show index effects.",
        users=60,
        seller_fraction=0.25,
        products=150,
        orders=200,
        max_items_per_order=4,
        reviews=300,
        posts=400,
        follows_per_user=5,
        history_days=90,
    ),
    "small": SeedProfile(
        name="small",
        description="Local development. Fast to build; index pathologies are visible but mild.",
        users=2_000,
        seller_fraction=0.15,
        products=6_000,
        orders=25_000,
        max_items_per_order=5,
        reviews=30_000,
        posts=50_000,
        follows_per_user=12,
        history_days=365,
    ),
    "medium": SeedProfile(
        name="medium",
        description="Default for evaluation runs. Index pathologies are unambiguous.",
        users=20_000,
        seller_fraction=0.12,
        products=60_000,
        orders=250_000,
        max_items_per_order=5,
        reviews=300_000,
        posts=500_000,
        follows_per_user=20,
        history_days=730,
    ),
    "large": SeedProfile(
        name="large",
        description="Headline scale: >1M orders and >1M posts. Expect a long build and >5GB on disk.",
        users=50_000,
        seller_fraction=0.10,
        products=200_000,
        orders=1_200_000,
        max_items_per_order=6,
        reviews=1_500_000,
        posts=1_500_000,
        follows_per_user=25,
        history_days=1095,
    ),
}


def get_profile(name: str) -> SeedProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown seed profile {name!r}. Choose one of: "
            f"{', '.join(sorted(PROFILES))}"
        ) from None
