"""Раз в неделю проверяет подписанные сериалы/фильмы (UserSeries.notify_releases=true)
и существующие watched/want_rewatch на новые сезоны. Шлёт юзеру уведомление если:

- появился новый сезон (seasons вырос)
- появилась/изменилась дата мировой или российской премьеры
- у запланированного фильма поменялся status_kp на «released» / «completed»

Дополнительно — ежедневный пуш «сегодня вышла новая серия» через
check_today_episodes (sched_daily): идёт по подписанным сериям, дёргает
kp.get_seasons и проверяет air_date == сегодня.
"""

from __future__ import annotations

import datetime as _dt
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

        # 4. Появился трейлер (раньше не было, теперь есть)
        new_yt = fresh.best_trailer_youtube_id
        if new_yt and new_yt != (s.trailer_youtube_id or ""):
            if not s.trailer_youtube_id:
                notifications.append(
                    f"🎥 У <b>{s.title_ru}</b> вышел трейлер!\n"
                    f"https://www.youtube.com/watch?v={new_yt}"
                )
            # Если ютуб-id поменялся — обычно тизер → официальный трейлер.
            # Тоже шлём, чтоб юзер увидел новую версию.
            else:
                notifications.append(
                    f"🎥 У <b>{s.title_ru}</b> вышел новый трейлер:\n"
                    f"https://www.youtube.com/watch?v={new_yt}"
                )

        # 5. Статус сменился на «вышел» (post-production → released / completed)
        new_status = (fresh.status_kp or "").lower()
        old_status = (s.status_kp or "").lower()
        if (
            new_status in ("released", "completed", "")
            and old_status in ("post-production", "pre-production", "announced", "filming", "in-production")
            and old_status
        ):
            notifications.append(
                f"🎬 <b>{s.title_ru}</b> вышел! Уже можно смотреть."
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
                if new_yt and new_yt != db_s.trailer_youtube_id:
                    db_s.trailer_youtube_id = new_yt
                    db_s.trailer_language = fresh.best_trailer_language
                    # Очищаем кэш file_id — старого файла нет, новый трейлер
                    db_s.trailer_file_id = None
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


async def check_today_episodes(
    bot: Bot, session_factory: async_sessionmaker, kp: KinopoiskClient,
) -> int:
    """Ежедневно: для всех подписанных сериалов смотрит расписание серий
    в KP и пушит «📺 Вышла серия X» по тем что выходят СЕГОДНЯ.
    """
    today = _dt.date.today()
    today_dmy = today.strftime("%d.%m.%Y")
    pings = 0
    async with session_factory() as session:
        stmt = (
            select(UserSeries.user_id, Series)
            .join(Series, UserSeries.series_id == Series.id)
            .where(
                or_(
                    UserSeries.notify_releases.is_(True),
                    UserSeries.status.in_(["watching", "want", "want_rewatch"]),
                ),
                Series.is_series.is_(True),
            )
        )
        rows = (await session.execute(stmt)).all()

    series_to_users: dict[int, list[int]] = {}
    series_obj: dict[int, Series] = {}
    for user_id, s in rows:
        series_to_users.setdefault(s.id, []).append(user_id)
        series_obj[s.id] = s

    for sid, user_ids in series_to_users.items():
        s = series_obj[sid]
        try:
            seasons = await kp.get_seasons(s.kp_id)
        except Exception as e:
            logger.warning("get_seasons for daily push %s failed: %s", s.kp_id, e)
            continue
        # Ищем эпизоды с air_date == сегодня
        today_eps: list[tuple[int, int, str]] = []  # (season_num, ep_num, name)
        for season in seasons:
            for ep in season.episodes:
                if ep.air_date == today_dmy:
                    today_eps.append((season.number, ep.number, ep.name or ""))
        if not today_eps:
            continue

        for season_num, ep_num, name in today_eps:
            name_part = f" «{name}»" if name else ""
            text = (
                f"📺 У <b>{s.title_ru}</b> сегодня вышла серия!\n"
                f"S{season_num}E{ep_num}{name_part}"
            )
            for uid in user_ids:
                try:
                    await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                    pings += 1
                except Exception as e:
                    logger.warning("today-ep push %s/%s failed: %s", uid, sid, e)

    logger.info("Daily episode pings: %d", pings)
    return pings
