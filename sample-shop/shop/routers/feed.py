"""Community surface: the personalised feed and individual posts."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from shop.dependencies import CurrentUser, DbSession, PageParams
from shop.models import Follow, Post, Product
from shop.schemas import Page, PostCreate, PostOut, UserSummary

logger = logging.getLogger("shop.feed")

router = APIRouter(prefix="/api", tags=["community"])


def _post_out(post: Post) -> PostOut:
    return PostOut(
        id=post.id,
        title=post.title,
        body=post.body,
        like_count=post.like_count,
        is_published=post.is_published,
        created_at=post.created_at,
        author=UserSummary.model_validate(post.author) if post.author else None,
        product_id=post.product_id,
    )


@router.get("/feed", response_model=Page[PostOut])
async def get_feed(
    db: DbSession,
    page: PageParams,
    user: CurrentUser,
) -> Page[PostOut]:
    """Posts from the sellers and buyers this user follows, newest first.

    ---------------------------------------------------------------------
    PLANTED PATHOLOGY P4 - missing composite index on posts.

    The follow lookup is fast: it reads the composite primary key of
    `follows`. The second query is the problem:

        WHERE author_id = ANY(:ids) AND is_published
        ORDER BY created_at DESC LIMIT :n

    The index this wants is (author_id, created_at DESC). `posts` has no
    secondary index at all, so PostgreSQL reads the table, discards the very
    large majority of rows on the author filter, and then sorts what is left.
    The filter is highly selective - a user follows tens of authors out of
    thousands - which is exactly the condition under which detector D2 should
    recommend an index and the selectivity check should agree.

    Do not add an index on posts. It is planted.
    See PATHOLOGIES.md (P4).
    ---------------------------------------------------------------------
    """
    followed_stmt = select(Follow.followed_id).where(Follow.follower_id == user.id)
    followed_ids = list((await db.execute(followed_stmt)).scalars().all())

    if not followed_ids:
        return Page(items=[], limit=page.limit, offset=page.offset, total=0)

    post_stmt = (
        select(Post)
        .options(selectinload(Post.author))
        .where(Post.author_id.in_(followed_ids), Post.is_published.is_(True))
        .order_by(Post.created_at.desc(), Post.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    posts = list((await db.execute(post_stmt)).scalars().all())

    return Page(
        items=[_post_out(p) for p in posts],
        limit=page.limit,
        offset=page.offset,
        total=None,
    )


@router.get("/posts/{post_id}", response_model=PostOut)
async def get_post(post_id: int, db: DbSession) -> PostOut:
    """Post permalink. Primary-key read plus one author load; control endpoint."""
    stmt = (
        select(Post).options(selectinload(Post.author)).where(Post.id == post_id)
    )
    post = (await db.execute(stmt)).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such post")
    return _post_out(post)


@router.post("/posts", response_model=PostOut, status_code=status.HTTP_201_CREATED)
async def create_post(
    payload: PostCreate,
    db: DbSession,
    user: CurrentUser,
) -> PostOut:
    """Publish a post, optionally attached to a product. Control endpoint."""
    if payload.product_id is not None:
        product = await db.get(Product, payload.product_id)
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such product"
            )

    post = Post(
        author_id=user.id,
        product_id=payload.product_id,
        title=payload.title,
        body=payload.body,
        is_published=True,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)

    logger.info("post created", extra={"post_id": post.id, "author_id": user.id})

    return PostOut(
        id=post.id,
        title=post.title,
        body=post.body,
        like_count=post.like_count,
        is_published=post.is_published,
        created_at=post.created_at,
        author=UserSummary.model_validate(user),
        product_id=post.product_id,
    )
