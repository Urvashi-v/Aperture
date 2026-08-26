"""SQLAlchemy ORM models for sample-shop.

The schema is part of the experiment, not just plumbing: two of the eight
planted pathologies are *absent* indexes (P3, P4). Every index below is
therefore a deliberate choice, and the indexes that are deliberately missing
are called out in comments that point at PATHOLOGIES.md. Adding one of them
casually would silently destroy an evaluation control.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base. Alembic autogenerate reads metadata from here."""


# Order lifecycle. Stored as text with a CHECK constraint rather than a native
# PostgreSQL ENUM: adding an enum value later requires an ALTER TYPE that has
# awkward transactional behaviour, which is a real operational annoyance for
# something that changes as often as an order status vocabulary.
ORDER_STATUSES: tuple[str, ...] = (
    "pending",
    "paid",
    "shipped",
    "delivered",
    "cancelled",
    "refunded",
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # A seller is just a user who lists products; modelling sellers as a
    # separate table would be more normalised and less like how these
    # applications actually get built.
    is_seller: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, default="US")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    products: Mapped[list["Product"]] = relationship(back_populates="seller")
    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    reviews: Mapped[list["Review"]] = relationship(back_populates="author")
    posts: Mapped[list["Post"]] = relationship(back_populates="author")

    __table_args__ = (
        # Login and profile lookup path. Unique because the application treats
        # email as the natural key.
        Index("uq_users_email", "email", unique=True),
        # Seller directory browse. Sellers are a small fraction of all users,
        # so a partial index is both smaller and more selective.
        Index(
            "idx_users_seller_created",
            "created_at",
            postgresql_where=text("is_seller"),
        ),
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    products: Mapped[list["Product"]] = relationship(back_populates="category")

    __table_args__ = (Index("uq_categories_slug", "slug", unique=True),)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Money as integer minor units. Binary floats and money are a bad
    # combination, and NUMERIC would slow the arithmetic in reporting queries
    # for no benefit at this precision.
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    stock_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Denormalised rating rollup, maintained when a review is written.
    # Recomputing it per catalogue page would be its own performance problem
    # and would confound the pathologies we actually want to study.
    rating_sum: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    seller: Mapped["User"] = relationship(back_populates="products")
    category: Mapped["Category"] = relationship(back_populates="products")
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("uq_products_sku", "sku", unique=True),
        # Catalogue browse: filter by category, active rows only, newest first.
        # This is a CONTROL path and is indexed correctly on purpose.
        Index("idx_products_category_created", "category_id", "created_at"),
        # Seller storefront listing.
        Index("idx_products_seller", "seller_id"),
        # Trigram index for the catalogue search box, which runs ILIKE on
        # title. Without it that filter sequentially scans the whole catalogue
        # and swamps the pathologies we are trying to isolate.
        Index(
            "idx_products_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        CheckConstraint("price_cents >= 0", name="ck_products_price_non_negative"),
        CheckConstraint("stock_qty >= 0", name="ck_products_stock_non_negative"),
        CheckConstraint("rating_count >= 0", name="ck_products_rating_count"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    shipping_country: Mapped[str] = mapped_column(
        String(2), nullable=False, default="US"
    )
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="orders")
    # order_by is not cosmetic: the fulfilment queue orders line items
    # explicitly while the order-detail endpoint reads them through this
    # relationship. Without it the two endpoints return the same line items in
    # different orders, which is a real client-visible inconsistency.
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItem.id",
    )

    __table_args__ = (
        # -------------------------------------------------------------------
        # PLANTED PATHOLOGY P3 - missing index on orders.user_id.
        #
        # PostgreSQL does not create an index for a foreign key on the
        # referencing side. Forgetting one here is the single most common
        # index bug in production ORM applications, which is exactly why it is
        # the planted case. GET /api/users/{id}/orders therefore sequentially
        # scans the orders table.
        #
        # DO NOT ADD idx_orders_user_id. See PATHOLOGIES.md (P3).
        # -------------------------------------------------------------------
        # Operational listing for the admin order queue - legitimately indexed.
        Index("idx_orders_placed_at", "placed_at"),
        CheckConstraint(
            "status IN "
            "('pending', 'paid', 'shipped', 'delivered', 'cancelled', 'refunded')",
            name="ck_orders_status",
        ),
        CheckConstraint("total_cents >= 0", name="ck_orders_total_non_negative"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()

    __table_args__ = (
        # Indexed on purpose. P1 (the N+1 on GET /api/orders) has to be a bug
        # about the NUMBER of round trips, not about each round trip being
        # slow. If this index were missing, the N+1 detector and the
        # missing-index detector would fire on the same root cause and the
        # evaluation could not tell which one was right.
        Index("idx_order_items_order", "order_id"),
        Index("idx_order_items_product", "product_id"),
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint(
            "unit_price_cents >= 0", name="ck_order_items_price_non_negative"
        ),
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    product: Mapped["Product"] = relationship(back_populates="reviews")
    author: Mapped["User"] = relationship(back_populates="reviews")

    __table_args__ = (
        # The product detail page reads reviews newest-first. Indexed, because
        # P2 (the N+1 on review authors) must be about round-trip count only.
        Index("idx_reviews_product_created", "product_id", "created_at"),
        Index("idx_reviews_author", "author_id"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating_range"),
    )


class Post(Base):
    """A community feed post.

    The shop has a social surface: sellers announce drops and restocks, buyers
    write buying guides. It is the read-heaviest part of the product and the
    natural home for the feed pathology.
    """

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # A post may or may not be about a specific product.
    product_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    author: Mapped["User"] = relationship(back_populates="posts")
    product: Mapped["Product | None"] = relationship()

    __table_args__ = (
        # -------------------------------------------------------------------
        # PLANTED PATHOLOGY P4 - missing composite index for the feed.
        #
        # GET /api/feed runs, in effect:
        #   WHERE author_id = ANY(:ids) AND is_published
        #   ORDER BY created_at DESC LIMIT :n
        # The correct index is (author_id, created_at DESC). It is deliberately
        # absent, so the planner gathers a large candidate set and sorts it.
        #
        # DO NOT ADD idx_posts_author_created. See PATHOLOGIES.md (P4).
        # -------------------------------------------------------------------
        # Nothing else is indexed here on purpose either: the only other read
        # of this table is the permalink, which goes through the primary key.
        # An index with no reader is dead weight that also perturbs the
        # planner's cost estimates for the feed query.
    )


class Follow(Base):
    """Who follows whom on the community side of the shop.

    Exists so the personalised feed has an author set to filter on. Deriving
    that set from the user's order history instead would make GET /api/feed
    read `orders.user_id`, and it would then trip planted pathology P3 as well
    as P4 - two causes on one endpoint, which would make the evaluation unable
    to attribute a finding to a cause.
    """

    __tablename__ = "follows"

    follower_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    followed_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The composite primary key already indexes (follower_id, followed_id),
        # which is the direction the feed reads. The reverse direction
        # (follower counts) needs its own index.
        Index("idx_follows_followed", "followed_id"),
        CheckConstraint("follower_id <> followed_id", name="ck_follows_not_self"),
    )
