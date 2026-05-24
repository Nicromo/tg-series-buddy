"""Entry point: aiogram bot polling + aiohttp /health + weekly check-in scheduler."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiohttp import web

from .config import Settings
from .db.repository import init_db, make_engine, make_session_factory
from .handlers import series as series_handlers
from .handlers import start as start_handlers
from .services.kinopoisk import KinopoiskClient
from .services.scheduler import start_scheduler


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

    port = int(os.getenv("PORT", "8080"))
    http_runner = await start_http_server(port)

    scheduler = start_scheduler(bot, session_factory)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await http_runner.cleanup()
        await kp.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
