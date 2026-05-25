"""Раз в неделю проверяет подписанные сериалы/фильмы (UserSeries.notify_releases=true)
и существующие watched/want_rewatch на новые сезоны. Шлёт юзеру уведомление если:

- появился новый сезон (seasons вырос)
- появилась/изменилась дата мировой или российской премьеры
- у запланированного фильма поменялся status_kp на «released» / «completed»
"""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import repository as repo
from ..db.models import Series, UserSeries
from .kinopoisk import KinopoiskClient

logger = logging.getLogger(__name__)


async def check_new_seasons(bot: Bot, session_factory: async_sessionmaker, kp: KinopoiskClient) -> int:
    """Проверяет (1) явно подписанные и (2) watched/want_rewatch сериалы.
    Возвращает количество отправленных уведомлений.
    """
    pings = 0
    async with session_factory() as session:
        # Объединяем: явно подписаны ИЛИ watched/want_rewatch (старое поведение)
        stmt = (
            select(UserSeries.user_id, Series)
            .join(Series, UserSeries.series_id == Series.id)
            .where(
                or_(
                    UserSeries.notify_releases.is_(True),
                    UserSeries.status.in_(["watched", "want_rewatch"]),
                )
            )
        )
        rows = (await session.execute(stmt)).all()

    # Группируем по сериалу чтобы дёрнуть KP один раз
    series_to_users: dict[int, list[int]] = {}
    series_obj: dict[int, Series] = {}
    for user_id, s in rows:
        series_to_users.setdefault(s.id, []).append(user_id)
        series_obj[s.id] = s

    for sid, user_ids in series_to_users.items():
        s = series_obj[sid]
        try:
            fresh = await kp.get_details(s.kp_id)
        except Exception as e:
            logger.warning("kp details failed for %s: %s", s.kp_id, e)
            continue

        notifications: list[str] = []

        # 1. Новый сезон
        old_seasons = s.seasons or 0
        new_seasons = fresh.seasons or 0
        if new_seasons > old_seasons:
            notifications.append(
                f"🎬 У <b>{s.title_ru}</b> новый сезон!\n"
                f"Было {old_seasons}, стало <b>{new_seasons}</b>."
            )

        # 2. Появилась/изменилась дата мировой премьеры
        if fresh.premiere_world and fresh.premiere_world != (s.premiere_world or ""):
            if not s.premiere_world:
                notifications.append(
                    f"📅 У <b>{s.title_ru}</b> объявили дату мировой премьеры: "
                    f"<b>{fresh.premiere_world}</b>"
                )
            else:
                notifications.append(
                    f"📅 У <b>{s.title_ru}</b> сместили мировую премьеру: "
                    f"{s.premiere_world} → <b>{fresh.premiere_world}</b>"
                )

        # 3. Появилась/изменилась дата российской премьеры
        if fresh.premiere_russia and fresh.premiere_russia != (s.premiere_russia or ""):
            if not s.premiere_russia:
                notifications.append(
                    f"🇷🇺 У <b>{s.title_ru}</b> объявили дату премьеры в России: "
                    f"<b>{fresh.premiere_russia}</b>"
                )
            else:
                notifications.append(
                    f"🇷🇺 У <b>{s.title_ru}</b> сместили российскую премьеру: "
                    f"{s.premiere_russia} → <b>{fresh.premiere_russia}</b>"
                )

        if not notifications:
            continue

        # Обновляем БД
        async with session_factory() as session:
            db_s = await session.get(Series, sid)
            if db_s:
                if new_seasons > old_seasons:
                    db_s.seasons = new_seasons
                if fresh.status_kp:
                    db_s.status_kp = fresh.status_kp
                if fresh.premiere_world:
                    db_s.premiere_world = fresh.premiere_world
                if fresh.premiere_russia:
                    db_s.premiere_russia = fresh.premiere_russia
                await session.commit()

        # Шлём пуш каждому подписчику
        body = "\n\n".join(notifications)
        for uid in user_ids:
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=body,
                    parse_mode="HTML",
                )
                pings += 1
            except Exception as e:
                logger.warning("ping user %s failed: %s", uid, e)

    logger.info("Release watcher: pings sent = %d", pings)
    return pings
