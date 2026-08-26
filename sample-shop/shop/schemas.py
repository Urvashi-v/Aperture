"""Pydantic request/response models.

Response shapes are kept close to what a real client would need, because the
shape drives the query pattern. `ProductDetail` embeds review authors, for
example, and that embedding is precisely what makes the N+1 on the product
page (P2) a natural mistake rather than a contrived one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class Page(BaseModel, Generic[T]):
    """Limit/offset page.

    Offset pagination, not cursors, because it is what the majority of CRUD
    services actually ship and because deep offsets are themselves a realistic
    source of latency.
    """

    items: list[T]
    limit: int
    offset: int
    total: int | None = Field(
        default=None,
        description=(
            "Total matching rows. Only returned when the caller asks for it, "
            "since the COUNT is a second query."
        ),
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserSummary(ORMModel):
    id: int
    display_name: str
    is_seller: bool


class UserOut(ORMModel):
    id: int
    email: str
    display_name: str
    is_seller: bool
    country_code: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


class CategoryOut(ORMModel):
    id: int
    slug: str
    name: str


class ProductSummary(ORMModel):
    id: int
    sku: str
    title: str
    price_cents: int
    currency: str
    is_active: bool
    rating_count: int
    average_rating: float | None = None


class ReviewOut(ORMModel):
    id: int
    product_id: int
    rating: int
    title: str
    body: str
    created_at: datetime
    author: UserSummary | None = None


class ProductDetail(ORMModel):
    id: int
    sku: str
    title: str
    description: str
    price_cents: int
    currency: str
    stock_qty: int
    is_active: bool
    category: CategoryOut
    seller: UserSummary
    rating_count: int
    average_rating: float | None = None
    recent_reviews: list[ReviewOut] = Field(default_factory=list)


class ReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class OrderItemOut(ORMModel):
    id: int
    product_id: int
    quantity: int
    unit_price_cents: int
    product_title: str | None = None
    product_sku: str | None = None


class OrderSummary(ORMModel):
    id: int
    status: str
    total_cents: int
    currency: str
    placed_at: datetime
    item_count: int = 0


class OrderOut(ORMModel):
    id: int
    user_id: int
    status: str
    total_cents: int
    currency: str
    shipping_country: str
    placed_at: datetime
    items: list[OrderItemOut] = Field(default_factory=list)


class OrderLineCreate(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=100)


class OrderCreate(BaseModel):
    items: list[OrderLineCreate] = Field(min_length=1, max_length=50)
    shipping_country: str = Field(default="US", min_length=2, max_length=2)


# ---------------------------------------------------------------------------
# Community feed
# ---------------------------------------------------------------------------


class PostOut(ORMModel):
    id: int
    title: str
    body: str
    like_count: int
    is_published: bool
    created_at: datetime
    author: UserSummary | None = None
    product_id: int | None = None


class PostCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=8000)
    product_id: int | None = None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class HealthOut(BaseModel):
    status: str
    service: str
    version: str


class ReadinessOut(BaseModel):
    status: str
    database: dict[str, object]


class InfoOut(BaseModel):
    service: str
    version: str
    config: dict[str, object]


class AdminStatsOut(BaseModel):
    users: int
    sellers: int
    products: int
    orders: int
    order_items: int
    reviews: int
    posts: int
    gross_merchandise_value_cents: int
