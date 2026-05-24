"""Thin layer of DB operations."""

from __future__ import annotations

import secrets
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base, Pair, Series, User, UserSeries


def make_engine(db_path: str):
    """Create async engine for SQLite at the given path."""
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False, future=True)


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------- Users / Pairs ----------

async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: Optional[str],
    full_name: Optional[str],
) -> User:
    user = await session.get(User, tg_id)
    if user is None:
        user = User(id=tg_id, username=username, full_name=full_name)
        session.add(user)
        await session.flush()
    else:
        user.username = username or user.username
        user.full_name = full_name or user.full_name
    return user


async def create_pair_for_user(session: AsyncSession, user: User) -> Pair:
    """Create a new pair with an invite code and attach the user to it."""
    invite = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")[:10]
    pair = Pair(invite_code=invite)
    session.add(pair)
    await session.flush()
    user.pair_id = pair.id
    await session.flush()
    return pair


async def join_pair_by_code(
    session: AsyncSession, user: User, invite_code: str
) -> Optional[Pair]:
    result = await session.execute(select(Pair).where(Pair.invite_code == invite_code))
    pair = result.scalar_one_or_none()
    if pair is None:
        return None
    user.pair_id = pair.id
    await session.flush()
    return pair


async def get_pair_members(session: AsyncSession, pair_id: int) -> Sequence[User]:
    result = await session.execute(select(User).where(User.pair_id == pair_id))
    return result.scalars().all()


# ---------- Series ----------

async def get_series_by_kp_id(session: AsyncSession, kp_id: int) -> Optional[Series]:
    result = await session.execute(select(Series).where(Series.kp_id == kp_id))
    return result.scalar_one_or_none()


async def upsert_series_from_dict(session: AsyncSession, data: dict) -> Series:
    """Create or update a series by kp_id."""
    existing = await get_series_by_kp_id(session, data["kp_id"])
    if existing:
        for k, v in data.items():
            if v is not None:
                setattr(existing, k, v)
        await session.flush()
        return existing
    series = Series(**data)
    session.add(series)
    await session.flush()
    return series


async def update_series(session: AsyncSession, series: Series, **fields) -> None:
    for k, v in fields.items():
        setattr(series, k, v)
    await session.flush()


# ---------- UserSeries ----------

async def set_user_series_status(
    session: AsyncSession, user_id: int, series_id: int, status: str
) -> UserSeries:
    result = await session.execute(
        select(UserSeries).where(
            UserSeries.user_id == user_id, UserSeries.series_id == series_id
        )
    )
    us = result.scalar_one_or_none()
    if us is None:
        us = UserSeries(user_id=user_id, series_id=series_id, status=status)
        session.add(us)
    else:
        us.status = status
    await session.flush()
    return us


async def set_user_series_rating(
    session: AsyncSession, user_id: int, series_id: int, rating: str
) -> UserSeries:
    result = await session.execute(
        select(UserSeries).where(
            UserSeries.user_id == user_id, UserSeries.series_id == series_id
        )
    )
    us = result.scalar_one_or_none()
    if us is None:
        us = UserSeries(user_id=user_id, series_id=series_id, status="want", rating=rating)
        session.add(us)
    else:
        us.rating = rating
    await session.flush()
    return us


async def list_user_series(
    session: AsyncSession, user_id: int, status: Optional[str] = None
) -> Sequence[tuple[UserSeries, Series]]:
    stmt = (
        select(UserSeries, Series)
        .join(Series, UserSeries.series_id == Series.id)
        .where(UserSeries.user_id == user_id)
        .order_by(UserSeries.updated_at.desc())
    )
    if status:
        stmt = stmt.where(UserSeries.status == status)
    result = await session.execute(stmt)
    return result.all()


async def list_pair_matches(
    session: AsyncSession, pair_id: int
) -> Sequence[Series]:
    """Series both pair members liked."""
    members = await get_pair_members(session, pair_id)
    if len(members) < 2:
        return []
    user_ids = [m.id for m in members]
    stmt = (
        select(Series)
        .join(UserSeries, UserSeries.series_id == Series.id)
        .where(UserSeries.user_id.in_(user_ids), UserSeries.rating == "like")
        .group_by(Series.id)
        .having(func.count(UserSeries.user_id.distinct()) == len(user_ids))
    )
    result = await session.execute(stmt)
    return result.scalars().all()
