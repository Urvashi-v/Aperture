"""Shared FastAPI dependencies.

Authentication note: sample-shop identifies the caller with an `X-User-Id`
header instead of a real session or JWT. This is a benchmark application whose
job is to produce realistic *database* traffic, and a full auth stack would add
surface area without changing a single query pattern. It is not a security
mechanism and is documented as such in the README. The one thing it does do
faithfully is what a real session middleware does: one primary-key lookup of
the caller per request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from shop.db import get_db
from shop.models import User

DbSession = Annotated[AsyncSession, Depends(get_db)]

# Catalogue and feed pages are capped. The cap exists so that the *only*
# unbounded read in the application is the deliberately planted one (P7).
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


async def get_current_user(
    db: DbSession,
    x_user_id: Annotated[
        int | None,
        Header(alias="X-User-Id", description="Caller's user id (benchmark auth)"),
    ] = None,
) -> User:
    if x_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-Id header is required",
        )

    user = await db.get(User, x_user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown user",
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


class Pagination:
    """Limit/offset pair, validated once instead of at every call site."""

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


PageParams = Annotated[Pagination, Depends(Pagination)]
