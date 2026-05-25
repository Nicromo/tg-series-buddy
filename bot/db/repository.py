"""Thin layer of DB operations."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base, BlacklistedGenre, Pair, Series, User, UserSeries, YoutubeSubscription, utcnow


def make_engine(db_url: str):
    """Create async engine. Accepts either sqlite+aiosqlite:/// or postgresql+asyncpg://"""
    kwargs = {"echo": False, "future": True}
    if db_url.startswith("postgresql"):
        # Neon/Supabase: SSL + disable asyncpg prepared statement cache (PgBouncer compat)
        kwargs["connect_args"] = {
            "ssl": True,
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }
        kwargs["pool_pre_ping"] = True
    return create_async_engine(db_url, **kwargs)


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Best-effort migration (works on both SQLite and Postgres)
        for sql in [
            "ALTER TABLE user_series ADD COLUMN IF NOT EXISTS last_checkin_at TIMESTAMP",
            "ALTER TABLE series ADD COLUMN IF NOT EXISTS watch_options_json TEXT",
            "ALTER TABLE user_series ADD COLUMN IF NOT EXISTS notify_releases BOOLEAN DEFAULT FALSE",
            "ALTER TABLE series ADD COLUMN IF NOT EXISTS premiere_world VARCHAR(16)",
            "ALTER TABLE series ADD COLUMN IF NOT EXISTS premiere_russia VARCHAR(16)",
            "ALTER TABLE series ADD COLUMN IF NOT EXISTS trailer_url VARCHAR(512)",
            "ALTER TABLE user_series ADD COLUMN IF NOT EXISTS current_episode VARCHAR(32)",
            "ALTER TABLE series ADD COLUMN IF NOT EXISTS is_series BOOLEAN DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS city VARCHAR(64)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS min_rating DOUBLE PRECISION",
        ]:
            try:
                await conn.exec_driver_sql(sql)
            except Exception:
                pass  # column already exists


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


async def sync_pair_series(session: AsyncSession, pair_id: int) -> int:
    """Зеркалит «хочу/смотрю/пересмотреть» между всеми членами пары.

    Идемпотентно: только ДОБАВЛЯЕТ недостающие UserSeries у партнёра,
    ничего не удаляет и не перетирает. Возвращает количество созданных
    новых записей. Перенос только активных статусов (watched/dropped —
    личные, не переносятся: один посмотрел, другой не обязан хотеть).
    """
    SHARED_STATUSES = ("want", "watching", "want_rewatch")
    members = await get_pair_members(session, pair_id)
    member_ids = [m.id for m in members]
    if len(member_ids) < 2:
        return 0

    # Все UserSeries по членам пары — за один запрос
    result = await session.execute(
        select(UserSeries).where(
            UserSeries.user_id.in_(member_ids),
            UserSeries.status.in_(SHARED_STATUSES),
        )
    )
    rows = result.scalars().all()
    # Для каждого series_id — у кого уже есть запись (любого статуса)
    have_any = set()
    rows_all = await session.execute(
        select(UserSeries.user_id, UserSeries.series_id).where(
            UserSeries.user_id.in_(member_ids)
        )
    )
    for uid, sid in rows_all.all():
        have_any.add((uid, sid))

    created = 0
    for row in rows:
        for target_uid in member_ids:
            if target_uid == row.user_id:
                continue
            if (target_uid, row.series_id) in have_any:
                continue
            session.add(UserSeries(
                user_id=target_uid,
                series_id=row.series_id,
                status=row.status,
            ))
            have_any.add((target_uid, row.series_id))
            created += 1
    if created:
        await session.flush()
    return created


async def list_all_users(session: AsyncSession) -> Sequence[User]:
    result = await session.execute(select(User))
    return result.scalars().all()


# ---------- Series ----------

async def get_series_by_kp_id(session: AsyncSession, kp_id: int) -> Optional[Series]:
    result = await session.execute(select(Series).where(Series.kp_id == kp_id))
    return result.scalar_one_or_none()


async def upsert_series_from_dict(session: AsyncSession, data: dict) -> Series:
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


# ---------- UserSeries ----------

async def _pair_member_ids(session: AsyncSession, user_id: int) -> list[int]:
    """Все user_id в одной паре с user_id (включая его). Если пары нет — [user_id]."""
    user = await session.get(User, user_id)
    if not user or not user.pair_id:
        return [user_id]
    members = await get_pair_members(session, user.pair_id)
    return [m.id for m in members]


async def set_user_series_status(
    session: AsyncSession, user_id: int, series_id: int, status: str
) -> UserSeries:
    """Применяет статус ко ВСЕМ членам пары (или только к самому юзеру, если
    пары нет). Возвращает UserSeries вызывающего."""
    member_ids = await _pair_member_ids(session, user_id)
    primary = None
    for uid in member_ids:
        result = await session.execute(
            select(UserSeries).where(
                UserSeries.user_id == uid, UserSeries.series_id == series_id
            )
        )
        us = result.scalar_one_or_none()
        if us is None:
            us = UserSeries(user_id=uid, series_id=series_id, status=status)
            session.add(us)
        else:
            us.status = status
        if uid == user_id:
            primary = us
    await session.flush()
    return primary


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


async def get_user_series(
    session: AsyncSession, user_id: int, series_id: int
) -> Optional[UserSeries]:
    result = await session.execute(
        select(UserSeries).where(
            UserSeries.user_id == user_id, UserSeries.series_id == series_id
        )
    )
    return result.scalar_one_or_none()


async def remove_user_series(
    session: AsyncSession, user_id: int, series_id: int
) -> bool:
    """Удаляет UserSeries у ВСЕХ членов пары — список общий. Сам Series
    в БД остаётся (может быть отсылка из других пар)."""
    member_ids = await _pair_member_ids(session, user_id)
    deleted = 0
    for uid in member_ids:
        us = await get_user_series(session, uid, series_id)
        if us is not None:
            await session.delete(us)
            deleted += 1
    if deleted:
        await session.flush()
    return deleted > 0


async def clear_user_series_rating(
    session: AsyncSession, user_id: int, series_id: int
) -> bool:
    """Отменяет лайк/дизлайк, статус и заметку оставляет."""
    us = await get_user_series(session, user_id, series_id)
    if us is None:
        return False
    us.rating = None
    await session.flush()
    return True


async def set_user_series_progress(
    session: AsyncSession, user_id: int, series_id: int, progress: Optional[str]
) -> Optional[UserSeries]:
    """Личный прогресс просмотра — НЕ зеркалится партнёру (каждый смотрит
    в своём темпе). None — сбросить."""
    us = await get_user_series(session, user_id, series_id)
    if us is None:
        us = UserSeries(user_id=user_id, series_id=series_id, status="watching", current_episode=progress)
        session.add(us)
    else:
        us.current_episode = progress
    await session.flush()
    return us


async def toggle_notify_releases(
    session: AsyncSession, user_id: int, series_id: int
) -> bool:
    """Включает/выключает подписку на новые сезоны/премьеру. Возвращает
    новое состояние. Создаёт UserSeries если ещё нет (с status=want).
    """
    us = await get_user_series(session, user_id, series_id)
    if us is None:
        us = UserSeries(user_id=user_id, series_id=series_id, status="want", notify_releases=True)
        session.add(us)
        await session.flush()
        return True
    us.notify_releases = not bool(us.notify_releases)
    await session.flush()
    return bool(us.notify_releases)


async def list_release_subscribers(session: AsyncSession) -> list[tuple[int, Series]]:
    """Список (user_id, Series) для всех подписанных на уведомления."""
    stmt = (
        select(UserSeries.user_id, Series)
        .join(Series, UserSeries.series_id == Series.id)
        .where(UserSeries.notify_releases.is_(True))
    )
    rows = (await session.execute(stmt)).all()
    return [(uid, s) for uid, s in rows]


# ---------- Blacklisted genres ----------

async def toggle_blacklisted_genre(
    session: AsyncSession, user: User, genre: str
) -> bool:
    """Toggle жанра в blacklist. Возвращает новое состояние (True=в blacklist).
    Общий для пары если есть pair_id, иначе личный."""
    scope_clause = (
        BlacklistedGenre.pair_id == user.pair_id if user.pair_id
        else (BlacklistedGenre.user_id == user.id) & BlacklistedGenre.pair_id.is_(None)
    )
    result = await session.execute(
        select(BlacklistedGenre).where(
            scope_clause, BlacklistedGenre.genre == genre,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        await session.delete(existing)
        await session.flush()
        return False
    session.add(BlacklistedGenre(
        pair_id=user.pair_id,
        user_id=None if user.pair_id else user.id,
        genre=genre,
    ))
    await session.flush()
    return True


async def list_blacklisted_genres(session: AsyncSession, user: User) -> list[str]:
    if user.pair_id:
        result = await session.execute(
            select(BlacklistedGenre.genre).where(BlacklistedGenre.pair_id == user.pair_id)
        )
    else:
        result = await session.execute(
            select(BlacklistedGenre.genre).where(
                BlacklistedGenre.user_id == user.id,
                BlacklistedGenre.pair_id.is_(None),
            )
        )
    return [g for (g,) in result.all()]


# ---------- YouTube subscriptions ----------

async def add_youtube_subscription(
    session: AsyncSession, user: User, channel_id: str, channel_title: str
) -> tuple[YoutubeSubscription, bool]:
    """Добавить подписку (общую для пары если есть pair_id, иначе личную).
    Возвращает (sub, created). created=False если уже была.
    """
    if user.pair_id:
        result = await session.execute(
            select(YoutubeSubscription).where(
                YoutubeSubscription.pair_id == user.pair_id,
                YoutubeSubscription.channel_id == channel_id,
            )
        )
    else:
        result = await session.execute(
            select(YoutubeSubscription).where(
                YoutubeSubscription.user_id == user.id,
                YoutubeSubscription.pair_id.is_(None),
                YoutubeSubscription.channel_id == channel_id,
            )
        )
    existing = result.scalar_one_or_none()
    if existing:
        # Обновим название на свежее
        existing.channel_title = channel_title
        await session.flush()
        return existing, False
    sub = YoutubeSubscription(
        pair_id=user.pair_id,
        user_id=None if user.pair_id else user.id,
        channel_id=channel_id,
        channel_title=channel_title,
    )
    session.add(sub)
    await session.flush()
    return sub, True


async def list_youtube_subscriptions(session: AsyncSession, user: User) -> list[YoutubeSubscription]:
    if user.pair_id:
        result = await session.execute(
            select(YoutubeSubscription).where(YoutubeSubscription.pair_id == user.pair_id)
            .order_by(YoutubeSubscription.created_at)
        )
    else:
        result = await session.execute(
            select(YoutubeSubscription).where(
                YoutubeSubscription.user_id == user.id,
                YoutubeSubscription.pair_id.is_(None),
            ).order_by(YoutubeSubscription.created_at)
        )
    return list(result.scalars().all())


async def remove_youtube_subscription(session: AsyncSession, sub_id: int) -> bool:
    sub = await session.get(YoutubeSubscription, sub_id)
    if sub is None:
        return False
    await session.delete(sub)
    await session.flush()
    return True


async def list_all_youtube_subscriptions(session: AsyncSession) -> list[YoutubeSubscription]:
    """Все подписки в системе — для scheduler-проверки."""
    result = await session.execute(select(YoutubeSubscription))
    return list(result.scalars().all())


async def mark_youtube_video_sent(
    session: AsyncSession, sub_id: int, video_id: str
) -> None:
    sub = await session.get(YoutubeSubscription, sub_id)
    if sub:
        sub.last_video_id = video_id
        await session.flush()


async def mark_checkin_sent(
    session: AsyncSession, user_id: int, series_id: int
) -> None:
    us = await get_user_series(session, user_id, series_id)
    if us:
        us.last_checkin_at = utcnow()
        await session.flush()


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


async def list_checkin_candidates(
    session: AsyncSession, *, older_than_days: int = 6
) -> Sequence[tuple[int, int, str]]:
    """Return (user_id, series_id, series_title) for everyone whose 'watching' series
    were last asked about >= older_than_days ago (or never)."""
    threshold = utcnow() - dt.timedelta(days=older_than_days)
    stmt = (
        select(UserSeries.user_id, Series.id, Series.title_ru)
        .join(Series, UserSeries.series_id == Series.id)
        .where(UserSeries.status == "watching")
        .where(
            (UserSeries.last_checkin_at.is_(None))
            | (UserSeries.last_checkin_at < threshold)
        )
    )
    result = await session.execute(stmt)
    return result.all()


async def list_pair_matches(
    session: AsyncSession, pair_id: int
) -> Sequence[Series]:
    """Series both pair members liked (only watched/want_rewatch)."""
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

async def bulk_set_status(
    session: AsyncSession, user_id: int, from_status: str, to_status: str
) -> int:
    """Перевести все user_series юзера со статусом from_status в to_status. Возвращает число изменённых."""
    result = await session.execute(
        select(UserSeries).where(
            UserSeries.user_id == user_id, UserSeries.status == from_status
        )
    )
    rows = result.scalars().all()
    for us in rows:
        us.status = to_status
    await session.flush()
    return len(rows)
