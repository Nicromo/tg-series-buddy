"""Entry point: starts aiogram bot polling + tiny aiohttp /health server in parallel.

The HTTP server is required for free hosting (Render Web Service expects a port).
Bot work happens via Telegram polling; HTTP is only for health checks / wake-ups.
"""

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

    # HTTP server for Render free Web Service (must bind to PORT)
    port = int(os.getenv("PORT", "8080"))
    http_runner = await start_http_server(port)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await http_runner.cleanup()
        await kp.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
