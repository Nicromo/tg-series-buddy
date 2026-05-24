"""Точка входа: aiogram polling + aiohttp /health + weekly check-in + Groq."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiohttp import web

from .config import Settings
from .db.repository import init_db, make_engine, make_session_factory
from .handlers import series as series_handlers
from .handlers import start as start_handlers
from .services.groq_ai import GroqClient
from .services.kinopoisk import KinopoiskClient
from .services.scheduler import start_scheduler


BOT_COMMANDS = [
    BotCommand(command="start", description="🚀 Запустить бота"),
    BotCommand(command="add", description="🎬 Добавить сериал"),
    BotCommand(command="today", description="🍿 Что включить сегодня"),
    BotCommand(command="list", description="👀 Что хотим посмотреть"),
    BotCommand(command="watching", description="▶️ Смотрим сейчас"),
    BotCommand(command="watched", description="✅ Досмотренные"),
    BotCommand(command="rewatch", description="🔁 Хотим пересмотреть"),
    BotCommand(command="random", description="🎲 Случайный из очереди"),
    BotCommand(command="match", description="💛 Что лайкнули вы оба"),
    BotCommand(command="suggest", description="✨ Подбор от ИИ"),
    BotCommand(command="swipe", description="🃏 Свайп-вечер"),
    BotCommand(command="find", description="🔎 Поиск в моих сериалах"),
    BotCommand(command="stats", description="📊 Статистика"),
    BotCommand(command="checkin", description="🔔 Спросить про активные"),
    BotCommand(command="pair", description="👫 Пара / инвайт-код"),
    BotCommand(command="menu", description="🧭 Показать меню"),
    BotCommand(command="help", description="ℹ️ Справка"),
]


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _root(_: web.Request) -> web.Response:
    return web.Response(text="series-bot is alive")


async def start_http_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _root)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("HTTP health server started on 0.0.0.0:%d", port)
    return runner


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    settings = Settings.from_env()
    logging.info(
        "Starting bot. DB: %s, API: %s, Groq: %s",
        settings.db_path, settings.kp_api_base, "yes" if settings.groq_api_key else "no",
    )

    engine = make_engine(str(settings.db_path))
    await init_db(engine)
    session_factory = make_session_factory(engine)

    kp = KinopoiskClient(settings.kp_api_key, base_url=settings.kp_api_base)
    groq = GroqClient(settings.groq_api_key, model=settings.groq_model) if settings.groq_api_key else None

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(start_handlers.make_router(session_factory))
    dp.include_router(series_handlers.make_router(session_factory, kp, settings, groq))

    port = int(os.getenv("PORT", "8080"))
    http_runner = await start_http_server(port)
    scheduler = start_scheduler(bot, session_factory)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands(BOT_COMMANDS)
        logging.info("Bot commands set: %d", len(BOT_COMMANDS))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await http_runner.cleanup()
        if groq:
            await groq.close()
        await kp.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
