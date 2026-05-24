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
    )
    if kp is not None:
        # Каждый понедельник 09:00 UTC — проверка новых сезонов
        scheduler.add_job(
            check_new_seasons,
            trigger=CronTrigger(day_of_week="mon", hour=9, minute=0),
            args=[bot, session_factory, kp],
            id="season_watcher",
            replace_existing=True,
        )
    scheduler.start()
    logger.info("Scheduler started: weekly_checkin Sun 19:00 UTC + season_watcher Mon 09:00 UTC")
    return scheduler
