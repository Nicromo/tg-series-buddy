"""Weekly check-in scheduler.

Runs a job every Sunday at 19:00 UTC (~22:00 Moscow time):
for every (user, series) pair with status='watching', send a message
asking how the user is doing with the series. Respects last_checkin_at
to avoid spamming if a manual /checkin was triggered recently.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import repository as repo
from ..keyboards.series_kb import checkin_keyboard
from .kinopoisk import KinopoiskClient
from .season_watcher import check_new_seasons
from .youtube_rss import fetch_latest_videos

logger = logging.getLogger(__name__)


async def run_weekly_checkin(bot: Bot, session_factory: async_sessionmaker) -> int:
    """Send weekly check-in to everyone with active 'watching' series.

    Returns the number of messages sent.
    """
    sent = 0
    async with session_factory() as session:
        candidates = await repo.list_checkin_candidates(session)

    for user_id, series_id, title in candidates:
        text = (
            f"📺 Прошла неделя!\n\n"
            f"Как у тебя дела с <b>{title}</b>?"
        )
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=checkin_keyboard(series_id),
            )
            sent += 1
            async with session_factory() as session:
                await repo.mark_checkin_sent(session, user_id, series_id)
                await session.commit()
        except Exception as e:
            # User might have blocked the bot; skip silently
            logger.warning("Check-in failed for user %s, series %s: %s", user_id, series_id, e)

    logger.info("Weekly check-in: sent %d messages", sent)
    return sent


async def check_youtube_subscriptions(bot: Bot, session_factory: async_sessionmaker) -> int:
    """Каждый час проходит по всем подпискам, шлёт новое видео если есть.
    Возвращает количество отправленных уведомлений.
    """
    pings = 0
    async with session_factory() as session:
        subs = await repo.list_all_youtube_subscriptions(session)

    # Группируем по channel_id (на одного youtube запрос на одну группу)
    by_channel: dict[str, list] = {}
    for s in subs:
        by_channel.setdefault(s.channel_id, []).append(s)

    for channel_id, subs_list in by_channel.items():
        videos = await fetch_latest_videos(channel_id, limit=1)
        if not videos:
            continue
        latest = videos[0]
        for sub in subs_list:
            if sub.last_video_id == latest.video_id:
                continue
            # Адресаты: pair_id → все юзеры пары, иначе sub.user_id
            recipients: list[int] = []
            if sub.pair_id:
                async with session_factory() as session:
                    members = await repo.get_pair_members(session, sub.pair_id)
                    recipients = [m.id for m in members]
            elif sub.user_id:
                recipients = [sub.user_id]
            for uid in recipients:
                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=(
                            f"📺 <b>{sub.channel_title}</b> · новое видео\n"
                            f"<b>{latest.title}</b>\n"
                            f"{latest.url}"
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    )
                    pings += 1
                except Exception as e:
                    logger.warning("YT push to %s failed: %s", uid, e)
            async with session_factory() as session:
                await repo.mark_youtube_video_sent(session, sub.id, latest.video_id)
                await session.commit()

    logger.info("YouTube subscriptions: pings sent = %d", pings)
    return pings


def start_scheduler(bot: Bot, session_factory: async_sessionmaker, kp: KinopoiskClient | None = None) -> AsyncIOScheduler:
    """Start APScheduler with the weekly check-in job. Returns the scheduler."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Sun 19:00 UTC = Sun 22:00 Moscow
    scheduler.add_job(
        run_weekly_checkin,
        trigger=CronTrigger(day_of_week="sun", hour=19, minute=0),
        args=[bot, session_factory],
        id="weekly_checkin",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    if kp is not None:
        # Каждый понедельник 09:00 UTC — проверка новых сезонов
        scheduler.add_job(
            check_new_seasons,
            trigger=CronTrigger(day_of_week="mon", hour=9, minute=0),
            args=[bot, session_factory, kp],
            id="season_watcher",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
    # Каждый час (в xx:17) — проверка YouTube подписок
    scheduler.add_job(
        check_youtube_subscriptions,
        trigger=CronTrigger(minute=17),
        args=[bot, session_factory],
        id="youtube_subs",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.start()
    logger.info(
        "Scheduler: weekly_checkin Sun 19:00 + season_watcher Mon 09:00 + youtube_subs hourly",
    )
    return scheduler
