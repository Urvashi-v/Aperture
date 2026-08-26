"""Seed generation: determinism, distribution shape, and referential validity.

These tests need no database. The generators are the part of the seeder most
likely to break silently - a subtle bug produces a dataset that still loads but
no longer has the distribution the evaluation depends on.
"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from shop.seed import generators as gen
from shop.seed.profiles import PROFILES, get_profile


def _rng(seed: int = 7) -> random.Random:
    return random.Random(seed)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_every_profile_is_internally_consistent() -> None:
    for profile in PROFILES.values():
        assert profile.users > 0
        assert 0 < profile.seller_fraction < 1
        assert int(profile.users * profile.seller_fraction) >= 1
        assert profile.max_items_per_order >= 1
        assert profile.follows_per_user < profile.users
        assert profile.history_days > 0


def test_profiles_increase_monotonically() -> None:
    order = ["tiny", "small", "medium", "large"]
    totals = [PROFILES[name].approx_total_rows() for name in order]
    assert totals == sorted(totals)


def test_large_profile_reaches_the_headline_scale() -> None:
    """DESIGN.md week 1 day 1 calls for >=1M rows in posts and orders."""
    large = PROFILES["large"]
    assert large.orders >= 1_000_000
    assert large.posts >= 1_000_000


def test_unknown_profile_is_rejected_with_a_useful_message() -> None:
    with pytest.raises(ValueError, match="tiny"):
        get_profile("enormous")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_produces_an_identical_dataset() -> None:
    now = gen.utc_now()
    first = list(gen.gen_products(_rng(99), 200, 20, now, 365))
    second = list(gen.gen_products(_rng(99), 200, 20, now, 365))
    assert first == second


def test_different_seeds_produce_different_data() -> None:
    now = gen.utc_now()
    first = list(gen.gen_products(_rng(1), 200, 20, now, 365))
    second = list(gen.gen_products(_rng(2), 200, 20, now, 365))
    assert first != second


# ---------------------------------------------------------------------------
# Row validity - anything violating these would be rejected by the schema
# ---------------------------------------------------------------------------


def test_categories_match_the_declared_specs() -> None:
    categories = list(gen.gen_categories())
    assert len(categories) == len(gen.CATEGORY_SPECS)
    assert [c[0] for c in categories] == list(range(1, len(categories) + 1))
    assert len({c[1] for c in categories}) == len(categories)  # slugs unique


def test_user_emails_are_unique_and_sellers_exist() -> None:
    now = gen.utc_now()
    users = list(gen.gen_users(_rng(), 500, 0.2, now, 365))
    assert len(users) == 500
    assert len({u[1] for u in users}) == 500
    sellers = [u for u in users if u[3]]
    assert len(sellers) == 100


def test_product_rows_satisfy_the_check_constraints() -> None:
    now = gen.utc_now()
    products = list(gen.gen_products(_rng(), 1000, 50, now, 365))
    assert len({p[3] for p in products}) == 1000  # sku unique
    for p in products:
        assert p[6] >= 0          # price_cents
        assert p[8] >= 0          # stock_qty
        assert 1 <= p[2] <= len(gen.CATEGORY_SPECS)
        assert 1 <= p[1] <= 50    # seller_id within range
        assert len(p[4]) <= 200   # title fits String(200)


def test_product_prices_span_a_realistic_range() -> None:
    now = gen.utc_now()
    prices = [p[6] for p in gen.gen_products(_rng(), 3000, 50, now, 365)]
    assert min(prices) < 2_000       # under $20 exists
    assert max(prices) > 100_000     # over $1000 exists


def test_order_statuses_are_all_valid() -> None:
    from shop.models import ORDER_STATUSES

    now = gen.utc_now()
    orders = list(gen.gen_orders(_rng(), 2000, 500, now, 365))
    assert {o[2] for o in orders} <= set(ORDER_STATUSES)
    assert all(1 <= o[1] <= 500 for o in orders)


def test_order_items_reference_valid_orders_and_products() -> None:
    prices = [100 * (i + 1) for i in range(200)]
    items = list(gen.gen_order_items(_rng(), 300, 200, 5, prices))
    assert items
    assert all(1 <= i[1] <= 300 for i in items)
    assert all(1 <= i[2] <= 200 for i in items)
    assert all(i[3] > 0 for i in items)
    assert all(i[4] == prices[i[2] - 1] for i in items)
    # ids are dense and ordered, which COPY into a BIGSERIAL column requires
    assert [i[0] for i in items] == list(range(1, len(items) + 1))


def test_no_order_repeats_a_product_line() -> None:
    """order_items has no unique constraint, but duplicate lines would be a bug."""
    prices = [100] * 50
    items = list(gen.gen_order_items(_rng(), 200, 50, 6, prices))
    seen: set[tuple[int, int]] = set()
    for item in items:
        key = (item[1], item[2])
        assert key not in seen
        seen.add(key)


def test_reviews_have_valid_ratings() -> None:
    now = gen.utc_now()
    reviews = list(gen.gen_reviews(_rng(), 2000, 300, 400, now, 365))
    assert all(1 <= r[3] <= 5 for r in reviews)
    assert all(1 <= r[1] <= 300 for r in reviews)
    assert all(1 <= r[2] <= 400 for r in reviews)


def test_ratings_skew_positive_like_a_real_marketplace() -> None:
    now = gen.utc_now()
    ratings = [r[3] for r in gen.gen_reviews(_rng(), 5000, 300, 400, now, 365)]
    high = sum(1 for r in ratings if r >= 4)
    assert high / len(ratings) > 0.6


def test_posts_reference_valid_authors_and_optional_products() -> None:
    now = gen.utc_now()
    posts = list(gen.gen_posts(_rng(), 2000, 500, 60, 300, now, 365))
    assert all(1 <= p[1] <= 500 for p in posts)
    assert all(p[2] is None or 1 <= p[2] <= 300 for p in posts)
    assert any(p[2] is None for p in posts)
    assert any(not p[5] for p in posts), "no drafts generated"


def test_follows_are_unique_and_never_self_referential() -> None:
    now = gen.utc_now()
    follows = list(gen.gen_follows(_rng(), 300, 40, 10, now, 365))
    pairs = {(f[0], f[1]) for f in follows}
    assert len(pairs) == len(follows)
    assert all(f[0] != f[1] for f in follows)


def test_follows_are_concentrated_on_sellers() -> None:
    """The feed query is only selective if follow targets are a small set."""
    now = gen.utc_now()
    follows = list(gen.gen_follows(_rng(), 1000, 100, 15, now, 365))
    seller_edges = sum(1 for f in follows if f[1] <= 100)
    assert seller_edges / len(follows) > 0.6


# ---------------------------------------------------------------------------
# Distribution shape
# ---------------------------------------------------------------------------


def test_order_distribution_is_skewed_not_uniform() -> None:
    """Some users must have far more orders than others.

    A uniform distribution would make every user's order history the same size
    and would flatten the selectivity that P3 depends on.
    """
    now = gen.utc_now()
    orders = list(gen.gen_orders(_rng(), 20_000, 1_000, now, 365))
    counts: dict[int, int] = {}
    for order in orders:
        counts[order[1]] = counts.get(order[1], 0) + 1
    busiest = max(counts.values())
    mean = len(orders) / len(counts)
    assert busiest > mean * 5


def test_timestamps_are_in_the_past_and_within_the_window() -> None:
    now = gen.utc_now()
    history_days = 200
    users = list(gen.gen_users(_rng(), 500, 0.2, now, history_days))
    oldest_allowed = now - timedelta(days=history_days + 1)
    for user in users:
        assert oldest_allowed <= user[5] <= now


def test_timestamps_are_timezone_aware() -> None:
    """asyncpg rejects naive datetimes for a timestamptz column."""
    now = gen.utc_now()
    user = next(gen.gen_users(_rng(), 5, 0.2, now, 30))
    assert user[5].tzinfo is not None
