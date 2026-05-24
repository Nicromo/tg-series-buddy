"""Точка входа: поднимает aiogram, БД, KP-клиент и роутеры."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .config import Settings
from .db.repository import init_db, make_engine, make_session_factory
from .handlers import series as series_handlers
from .handlers import start as start_handlers
from .services.kinopoisk import KinopoiskClient


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    settings = Settings.from_env()
    logging.info("Starting bot. DB: %s, API: %s", settings.db_path, settings.kp_api_base)

    engine = make_engine(str(settings.db_path))
    await init_db(engine)
    session_factory = make_session_factory(engine)

    kp = KinopoiskClient(settings.kp_api_key, base_url=settings.kp_api_base)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()

    dp.include_router(start_handlers.make_router(session_factory))
    dp.include_router(series_handlers.make_router(session_factory, kp, settings))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await kp.close()
        await bot.session.close()
        await eng