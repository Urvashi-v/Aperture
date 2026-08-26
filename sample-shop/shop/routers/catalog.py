"""Catalogue: categories, product browse, product detail, product reviews."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from shop.dependencies import CurrentUser, DbSession, PageParams
from shop.models import Category, Product, Review, User
from shop.schemas import (
    CategoryOut,
    Page,
    ProductDetail,
    ProductSummary,
    ReviewCreate,
    ReviewOut,
    UserSummary,
)

logger = logging.getLogger("shop.catalog")

router = APIRouter(prefix="/api", tags=["catalog"])

# How many reviews the product page shows inline before the reader has to open
# the full review list.
PRODUCT_PAGE_REVIEW_COUNT = 10


def _average_rating(product: Product) -> float | None:
    if product.rating_count == 0:
        return None
    return round(product.rating_sum / product.rating_count, 2)


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(db: DbSession) -> list[Category]:
    """Full category list. Small, bounded dimension table."""
    result = await db.execute(select(Category).order_by(Category.name))
    return list(result.scalars().all())


@router.get("/products", response_model=Page[ProductSummary])
async def browse_products(
    db: DbSession,
    page: PageParams,
    category_id: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=2, max_length=80)] = None,
    min_price_cents: Annotated[int | None, Query(ge=0)] = None,
    max_price_cents: Annotated[int | None, Query(ge=0)] = None,
    include_total: Annotated[bool, Query()] = False,
) -> Page[ProductSummary]:
    """Catalogue browse.

    Control endpoint: bounded page size, filters that the indexes in
    `models.Product.__table_args__` actually cover, and the COUNT is opt-in
    because it is a second full scan of the filtered set.
    """
    stmt = select(Product).where(Product.is_active.is_(True))

    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if q:
        # Backed by idx_products_title_trgm (GIN, gin_trgm_ops).
        stmt = stmt.where(Product.title.ilike(f"%{q}%"))
    if min_price_cents is not None:
        stmt = stmt.where(Product.price_cents >= min_price_cents)
    if max_price_cents is not None:
        stmt = stmt.where(Product.price_cents <= max_price_cents)

    total: int | None = None
    if include_total:
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(Product.created_at.desc(), Product.id.desc())
    stmt = stmt.limit(page.limit).offset(page.offset)

    products = list((await db.execute(stmt)).scalars().all())
    items = [
        ProductSummary(
            id=p.id,
            sku=p.sku,
            title=p.title,
            price_cents=p.price_cents,
            currency=p.currency,
            is_active=p.is_active,
            rating_count=p.rating_count,
            average_rating=_average_rating(p),
        )
        for p in products
    ]
    return Page(items=items, limit=page.limit, offset=page.offset, total=total)


@router.get("/products/{product_id}", response_model=ProductDetail)
async def get_product(product_id: int, db: DbSession) -> ProductDetail:
    """Product detail page: product, seller, category, and recent reviews.

    ---------------------------------------------------------------------
    PLANTED PATHOLOGY P2 - N+1 on review authors.

    The product and its category/seller are loaded correctly in one round
    trip. The reviews are then fetched, and the author of each review is
    resolved one at a time inside the loop below. That is the shape almost
    every ORM application arrives at first, because the reviews query and the
    author lookup are written at different times by different people.

    Do not "fix" this by adding `selectinload(Review.author)`. It is planted.
    See PATHOLOGIES.md (P2).
    ---------------------------------------------------------------------
    """
    stmt = (
        select(Product)
        .options(joinedload(Product.category), joinedload(Product.seller))
        .where(Product.id == product_id)
    )
    product = (await db.execute(stmt)).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such product")

    review_stmt = (
        select(Review)
        .where(Review.product_id == product_id)
        .order_by(Review.created_at.desc())
        .limit(PRODUCT_PAGE_REVIEW_COUNT)
    )
    reviews = list((await db.execute(review_stmt)).scalars().all())

    recent: list[ReviewOut] = []
    for review in reviews:
        # One primary-key query per review. This is the N+1.
        author = await db.get(User, review.author_id)
        recent.append(
            ReviewOut(
                id=review.id,
                product_id=review.product_id,
                rating=review.rating,
                title=review.title,
                body=review.body,
                created_at=review.created_at,
                author=UserSummary.model_validate(author) if author else None,
            )
        )

    return ProductDetail(
        id=product.id,
        sku=product.sku,
        title=product.title,
        description=product.description,
        price_cents=product.price_cents,
        currency=product.currency,
        stock_qty=product.stock_qty,
        is_active=product.is_active,
        category=CategoryOut.model_validate(product.category),
        seller=UserSummary.model_validate(product.seller),
        rating_count=product.rating_count,
        average_rating=_average_rating(product),
        recent_reviews=recent,
    )


@router.get("/products/{product_id}/reviews", response_model=Page[ReviewOut])
async def list_product_reviews(
    product_id: int,
    db: DbSession,
    page: PageParams,
) -> Page[ReviewOut]:
    """Paginated review list.

    Control endpoint. Same data as the inline reviews on the product page, but
    the authors are loaded in a single additional round trip with
    `selectinload`. Having the correct implementation of the same read living
    next to the pathological one is what makes P2 a controlled comparison
    rather than an anecdote.
    """
    stmt = (
        select(Review)
        .options(selectinload(Review.author))
        .where(Review.product_id == product_id)
        .order_by(Review.created_at.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    reviews = list((await db.execute(stmt)).scalars().all())
    items = [
        ReviewOut(
            id=r.id,
            product_id=r.product_id,
            rating=r.rating,
            title=r.title,
            body=r.body,
            created_at=r.created_at,
            author=UserSummary.model_validate(r.author) if r.author else None,
        )
        for r in reviews
    ]
    return Page(items=items, limit=page.limit, offset=page.offset, total=None)


@router.post(
    "/products/{product_id}/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_review(
    product_id: int,
    payload: ReviewCreate,
    db: DbSession,
    user: CurrentUser,
) -> ReviewOut:
    """Write a review and update the product's denormalised rating rollup."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such product")

    review = Review(
        product_id=product_id,
        author_id=user.id,
        rating=payload.rating,
        title=payload.title,
        body=payload.body,
    )
    db.add(review)

    product.rating_sum += payload.rating
    product.rating_count += 1

    await db.commit()
    await db.refresh(review)

    logger.info(
        "review created",
        extra={"review_id": review.id, "product_id": product_id, "author_id": user.id},
    )

    return ReviewOut(
        id=review.id,
        product_id=review.product_id,
        rating=review.rating,
        title=review.title,
        body=review.body,
        created_at=review.created_at,
        author=UserSummary.model_validate(user),
    )
