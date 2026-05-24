"""Раз в неделю проверяет watched/want_rewatch сериалы — если в KP появился новый сезон,
шлёт пользователю уведомление."""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db.models import Series, UserSeries
from .kinopoisk import KinopoiskClient

logger = logging.getLogger(__name__)


async def check_new_seasons(bot: Bot, session_factory: async_sessionmaker, kp: KinopoiskClient) -> int:
    """Проверяет все watched/want_rewatch сериалы. Если кол-во сезонов в KP > сохранённого — пингует юзера."""
    pings = 0
    async with session_factory() as session:
        stmt = (
            select(UserSeries.user_id, Series)
            .join(Series, UserSeries.series_id == Series.id)
            .where(UserSeries.status.in_(["watched", "want_rewatch"]))
        )
        rows = (await session.execute(stmt)).all()

    # Group by series to fetch each only once
    series_to_users: dict[int, list[int]] = {}
    series_obj: dict[int, Series] = {}
    for user_id, s in rows:
        series_to_users.setdefault(s.id, []).append(user_id)
        series_obj[s.id] = s

    for sid, user_ids in series_to_users.items():
        s = series_obj[sid]
        old_seasons = s.seasons or 0
        try:
            fresh = await kp.get_details(s.kp_id)
        except Exception as e:
            logger.warning("kp details failed for %s: %s", s.kp_id, e)
            continue
        new_seasons = fresh.seasons or 0
        if new_seasons > old_seasons:
            # Update DB
            async with session_factory() as session:
                db_s = await session.get(Series, sid)
                if db_s:
                    db_s.seasons = new_seasons
                    db_s.status_kp = fresh.status_kp
                    await session.commit()
            # Ping users
            for uid in user_ids:
                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=(
                            f"🎬 У <b>{s.title_ru}</b> появился новый сезон!\n"
                            f"Было {old_seasons}, стало <b>{new_seasons}</b>.\n\n"
                            f"Глянем? 👀"
                        ),
                        parse_mode="HTML",
                    )
                    pings += 1
                except Exception as e:
                    logger.warning("ping user %s failed: %s", uid, e)
    logger.info("Season watcher: pings sent = %d", pings)
    return pings
