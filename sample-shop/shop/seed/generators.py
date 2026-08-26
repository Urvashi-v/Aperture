"""Deterministic row generators for the seed dataset.

No Faker. Faker is a fine library, but it is roughly two orders of magnitude
too slow to generate millions of rows, and the realism it buys - genuinely
plausible personal names - is not realism this benchmark needs. What the
benchmark does need is: reproducibility from a seed, skewed rather than uniform
distributions (a few sellers own most products, a few products get most
reviews), and enough text volume that rows are a realistic size on disk.

Everything below is driven by a single `random.Random` instance, so a given
`--seed` reproduces the identical dataset.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

CATEGORY_SPECS: tuple[tuple[str, str], ...] = (
    ("home-kitchen", "Home & Kitchen"),
    ("furniture", "Furniture"),
    ("lighting", "Lighting"),
    ("outdoor", "Outdoor & Garden"),
    ("apparel", "Apparel"),
    ("footwear", "Footwear"),
    ("bags", "Bags & Luggage"),
    ("electronics", "Electronics"),
    ("audio", "Audio"),
    ("stationery", "Stationery"),
    ("pet-supplies", "Pet Supplies"),
    ("sports", "Sport & Fitness"),
)

_FIRST_NAMES = (
    "ava", "noah", "mia", "liam", "zoe", "kai", "ines", "omar", "lena", "hugo",
    "sana", "theo", "nina", "ravi", "elsa", "jonas", "maya", "pablo", "iris",
    "yuki", "amara", "felix", "nora", "dmitri", "leah", "tariq", "rosa", "sven",
)
_LAST_NAMES = (
    "turner", "okafor", "lindqvist", "moreau", "bianchi", "novak", "haddad",
    "ferreira", "kowalski", "yamamoto", "oyelaran", "petrov", "singh", "vargas",
    "mcallister", "nakamura", "dubois", "andersen", "rahman", "castillo",
)
_COUNTRIES = ("US", "GB", "DE", "FR", "CA", "AU", "IN", "BR", "JP", "SE", "NL", "ES")

_MATERIALS = (
    "Walnut", "Brushed Steel", "Linen", "Ceramic", "Recycled Cotton", "Oak",
    "Anodised Aluminium", "Cork", "Merino", "Borosilicate", "Rattan", "Canvas",
)
_QUALIFIERS = (
    "Compact", "Wide", "Low-Profile", "Insulated", "Foldable", "Modular",
    "Weatherproof", "Adjustable", "Stackable", "Lightweight", "Heavy-Duty",
)
_NOUNS_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "home-kitchen": ("Chef Knife", "Mixing Bowl", "Pour-Over Kettle", "Storage Jar"),
    "furniture": ("Side Table", "Bookcase", "Desk", "Stool", "Bed Frame"),
    "lighting": ("Desk Lamp", "Pendant Light", "Floor Lamp", "Wall Sconce"),
    "outdoor": ("Planter", "Watering Can", "Garden Trowel", "Patio Cover"),
    "apparel": ("Overshirt", "Chore Jacket", "Knit Sweater", "Rain Shell"),
    "footwear": ("Trail Runner", "Chelsea Boot", "Court Sneaker", "Sandal"),
    "bags": ("Weekender", "Backpack", "Tote", "Sling Pouch", "Cabin Case"),
    "electronics": ("USB-C Hub", "Power Bank", "Mechanical Keyboard", "Webcam"),
    "audio": ("Bookshelf Speaker", "Field Recorder", "Studio Monitor", "Earphones"),
    "stationery": ("Notebook", "Fountain Pen", "Desk Organiser", "Sketchbook"),
    "pet-supplies": ("Travel Bowl", "Rope Toy", "Dog Bed", "Grooming Brush"),
    "sports": ("Yoga Mat", "Kettlebell", "Foam Roller", "Water Bottle"),
}

_REVIEW_OPENERS = (
    "Arrived faster than the estimate.",
    "Second one I have bought.",
    "Held up through a full winter.",
    "Not quite what the photos suggested.",
    "Exactly the size I needed.",
    "Packaging was minimal, which I appreciated.",
    "Took a while to get used to.",
)
_REVIEW_BODIES = (
    "The finish is even and there were no sharp edges anywhere.",
    "Weight is noticeably lower than the one it replaced, which matters daily.",
    "One corner arrived scuffed but support sorted it out without argument.",
    "Assembly took about twenty minutes with the supplied hex key.",
    "It does the job, though the instructions were close to useless.",
    "After three months of near-daily use there is no visible wear.",
    "Fit is generous; consider sizing down if you are between sizes.",
)

_POST_TEMPLATES = (
    "Restock: {subject} is back",
    "Why we changed the {subject} spec",
    "Behind the {subject} drop",
    "Five ways we use the {subject}",
    "{subject}: what a year of use looks like",
    "Field notes on the {subject}",
)
_POST_PARAGRAPHS = (
    "We ran a small batch first to see whether the tolerances held, and they did.",
    "The supplier changed hands in spring, so every component was re-qualified.",
    "Most of the questions we get are about care, so here is the short version.",
    "It is not the cheapest option and we are not going to pretend otherwise.",
    "Two customers sent photos after a full season outdoors and nothing had lifted.",
    "If you are choosing between the two sizes, the smaller one covers most cases.",
)

ORDER_STATUS_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("delivered", 0.55),
    ("shipped", 0.15),
    ("paid", 0.12),
    ("pending", 0.10),
    ("cancelled", 0.05),
    ("refunded", 0.03),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _weighted_choice(rng: random.Random, weighted: tuple[tuple[str, float], ...]) -> str:
    roll = rng.random()
    cumulative = 0.0
    for value, weight in weighted:
        cumulative += weight
        if roll <= cumulative:
            return value
    return weighted[-1][0]


def _skewed_index(rng: random.Random, n: int) -> int:
    """Pick an index in [0, n) with a long-tail bias toward low indices.

    Real catalogues are not uniform: a small number of sellers own a large
    share of products, and a small number of products collect a large share of
    reviews. Uniform sampling would flatten that and make every query equally
    selective, which is not the workload we are trying to reproduce.
    """
    # Square of a uniform draw: cheap, no dependencies, gives a usable Pareto-ish shape.
    return min(n - 1, int(n * rng.random() ** 2))


def _timestamp(rng: random.Random, now: datetime, history_days: int) -> datetime:
    """A time in the past, weighted toward the recent end."""
    fraction = rng.random() ** 1.6  # recency bias
    seconds_back = fraction * history_days * 86_400
    return now - timedelta(seconds=seconds_back)


# ---------------------------------------------------------------------------
# Row generators. Each yields tuples in the exact column order the seeder
# passes to asyncpg's COPY.
# ---------------------------------------------------------------------------


def gen_categories() -> Iterator[tuple[int, str, str]]:
    """(id, slug, name)"""
    for index, (slug, name) in enumerate(CATEGORY_SPECS, start=1):
        yield index, slug, name


def gen_users(
    rng: random.Random, count: int, seller_fraction: float, now: datetime, history_days: int
) -> Iterator[tuple[int, str, str, bool, str, datetime]]:
    """(id, email, display_name, is_seller, country_code, created_at)

    The first `count * seller_fraction` ids are the sellers. Making sellers a
    contiguous id range keeps the seeder simple and costs nothing: no query in
    the application filters on id ranges.
    """
    seller_cutoff = int(count * seller_fraction)
    for user_id in range(1, count + 1):
        first = _FIRST_NAMES[rng.randrange(len(_FIRST_NAMES))]
        last = _LAST_NAMES[rng.randrange(len(_LAST_NAMES))]
        yield (
            user_id,
            f"{first}.{last}{user_id}@example.com",
            f"{first.capitalize()} {last.capitalize()}",
            user_id <= seller_cutoff,
            _COUNTRIES[rng.randrange(len(_COUNTRIES))],
            _timestamp(rng, now, history_days),
        )


def gen_products(
    rng: random.Random,
    count: int,
    seller_count: int,
    now: datetime,
    history_days: int,
) -> Iterator[tuple[int, int, int, str, str, str, int, str, int, bool, int, int, datetime]]:
    """(id, seller_id, category_id, sku, title, description, price_cents,
        currency, stock_qty, is_active, rating_sum, rating_count, created_at)

    rating_sum/rating_count are written as zero here and recomputed from the
    reviews table at the end of the seed run, so the rollup is always
    consistent with the rows that actually exist.
    """
    category_count = len(CATEGORY_SPECS)
    for product_id in range(1, count + 1):
        category_index = rng.randrange(category_count)
        slug, _ = CATEGORY_SPECS[category_index]
        nouns = _NOUNS_BY_CATEGORY[slug]

        title = (
            f"{_QUALIFIERS[rng.randrange(len(_QUALIFIERS))]} "
            f"{_MATERIALS[rng.randrange(len(_MATERIALS))]} "
            f"{nouns[rng.randrange(len(nouns))]}"
        )
        description = " ".join(
            rng.sample(_POST_PARAGRAPHS, k=min(3, len(_POST_PARAGRAPHS)))
        )
        # Log-uniform price between $4 and $900: catalogues are not uniform in
        # price either, and a realistic spread matters for the price filters.
        price_cents = int(400 * (2250 ** rng.random()))

        yield (
            product_id,
            # Skewed: a minority of sellers own the majority of the catalogue.
            _skewed_index(rng, seller_count) + 1,
            category_index + 1,
            f"{slug[:3].upper()}-{product_id:08d}",
            title,
            description,
            price_cents,
            "USD",
            rng.randrange(0, 500),
            rng.random() > 0.06,  # ~6% delisted
            0,
            0,
            _timestamp(rng, now, history_days),
        )


def gen_orders(
    rng: random.Random, count: int, user_count: int, now: datetime, history_days: int
) -> Iterator[tuple[int, int, str, int, str, str, datetime]]:
    """(id, user_id, status, total_cents, currency, shipping_country, placed_at)

    total_cents is written as zero and recomputed from order_items at the end.
    """
    for order_id in range(1, count + 1):
        yield (
            order_id,
            # Skewed: repeat customers place far more orders than one-off
            # buyers. This is what makes GET /api/users/{id}/orders interesting
            # - some users have three orders, some have hundreds.
            _skewed_index(rng, user_count) + 1,
            _weighted_choice(rng, ORDER_STATUS_WEIGHTS),
            0,
            "USD",
            _COUNTRIES[rng.randrange(len(_COUNTRIES))],
            _timestamp(rng, now, history_days),
        )


def gen_order_items(
    rng: random.Random,
    order_count: int,
    product_count: int,
    max_items: int,
    price_lookup: list[int],
) -> Iterator[tuple[int, int, int, int, int]]:
    """(id, order_id, product_id, quantity, unit_price_cents)

    `price_lookup` is indexed by product_id - 1 and holds the generated price,
    so line prices agree with the catalogue without a database round trip.
    """
    item_id = 0
    for order_id in range(1, order_count + 1):
        # Most orders are one or two lines; a few are large.
        line_count = 1 + int((max_items - 1) * rng.random() ** 2)
        chosen: set[int] = set()
        for _ in range(line_count):
            product_id = _skewed_index(rng, product_count) + 1
            if product_id in chosen:
                continue
            chosen.add(product_id)
            item_id += 1
            yield (
                item_id,
                order_id,
                product_id,
                1 + int(3 * rng.random() ** 3),
                price_lookup[product_id - 1],
            )


def gen_reviews(
    rng: random.Random,
    count: int,
    product_count: int,
    user_count: int,
    now: datetime,
    history_days: int,
) -> Iterator[tuple[int, int, int, int, str, str, datetime]]:
    """(id, product_id, author_id, rating, title, body, created_at)"""
    for review_id in range(1, count + 1):
        # Ratings skew high, as they do on every real marketplace.
        rating = _weighted_choice(
            rng,
            (("5", 0.48), ("4", 0.26), ("3", 0.13), ("2", 0.08), ("1", 0.05)),
        )
        body = " ".join(
            rng.sample(_REVIEW_BODIES, k=min(2 + rng.randrange(2), len(_REVIEW_BODIES)))
        )
        yield (
            review_id,
            _skewed_index(rng, product_count) + 1,
            rng.randrange(1, user_count + 1),
            int(rating),
            _REVIEW_OPENERS[rng.randrange(len(_REVIEW_OPENERS))],
            body,
            _timestamp(rng, now, history_days),
        )


def gen_posts(
    rng: random.Random,
    count: int,
    user_count: int,
    seller_count: int,
    product_count: int,
    now: datetime,
    history_days: int,
) -> Iterator[tuple[int, int, int | None, str, str, bool, int, datetime]]:
    """(id, author_id, product_id, title, body, is_published, like_count, created_at)"""
    noun_pool = tuple(
        noun for nouns in _NOUNS_BY_CATEGORY.values() for noun in nouns
    )
    for post_id in range(1, count + 1):
        # Sellers write most of the feed; buyers write the rest.
        if rng.random() < 0.7 and seller_count > 0:
            author_id = _skewed_index(rng, seller_count) + 1
        else:
            author_id = rng.randrange(1, user_count + 1)

        subject = noun_pool[rng.randrange(len(noun_pool))]
        body = " ".join(
            rng.sample(_POST_PARAGRAPHS, k=min(4, len(_POST_PARAGRAPHS)))
        )
        yield (
            post_id,
            author_id,
            (_skewed_index(rng, product_count) + 1) if rng.random() < 0.6 else None,
            _POST_TEMPLATES[rng.randrange(len(_POST_TEMPLATES))].format(subject=subject),
            body,
            rng.random() > 0.08,  # ~8% drafts
            int(200 * rng.random() ** 3),
            _timestamp(rng, now, history_days),
        )


def gen_follows(
    rng: random.Random,
    user_count: int,
    seller_count: int,
    per_user: int,
    now: datetime,
    history_days: int,
) -> Iterator[tuple[int, int, datetime]]:
    """(follower_id, followed_id, created_at)

    Users mostly follow sellers, which is what makes the feed query selective:
    a follower's author set is tens of ids out of tens of thousands of users.
    """
    for follower_id in range(1, user_count + 1):
        followed: set[int] = set()
        attempts = 0
        target = per_user
        while len(followed) < target and attempts < target * 4:
            attempts += 1
            if rng.random() < 0.8 and seller_count > 0:
                candidate = _skewed_index(rng, seller_count) + 1
            else:
                candidate = rng.randrange(1, user_count + 1)
            if candidate == follower_id:
                continue
            followed.add(candidate)
        for followed_id in followed:
            yield follower_id, followed_id, _timestamp(rng, now, history_days)


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)
