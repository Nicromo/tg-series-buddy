"""Точка входа: aiogram polling + aiohttp /health + weekly check-in + Groq."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
import httpx
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from .config import Settings
from .db.repository import init_db, make_engine, make_session_factory
from .handlers import series as series_handlers
from .handlers import start as start_handlers
from .services.groq_ai import GroqClient
from .services.kinopoisk import KinopoiskClient
from .services.scheduler import start_scheduler


BOT_COMMANDS = [
    BotCommand(command="today",    description="🍿 Что смотреть сегодня"),
    BotCommand(command="add",      description="➕ Добавить сериал или фильм"),
    BotCommand(command="list",     description="📋 Наш общий список «хочу»"),
    BotCommand(command="watching", description="▶️ Смотрим сейчас"),
    BotCommand(command="upcoming", description="📅 Премьеры под ваши жанры"),
    BotCommand(command="cinema",   description="🎫 Что в кино в твоём городе"),
    BotCommand(command="poll",     description="🗳 Выбираем вместе"),
    BotCommand(command="where",    description="📺 Где смотреть"),
    BotCommand(command="pair",     description="👫 Связаться с партнёром"),
    BotCommand(command="subs",     description="📺 YouTube подписки"),
    BotCommand(command="suggest",  description="🪄 Подбор от ИИ"),
    BotCommand(command="blacklist", description="🚫 Жанры-табу для ИИ"),
    BotCommand(command="swipe",    description="🃏 Свайп-вечер"),
    BotCommand(command="stats",    description="📊 Статистика"),
    BotCommand(command="wallpaper",description="📸 Постер «Наша неделя»"),
    BotCommand(command="donate",   description="💛 Поддержать бота"),
    BotCommand(command="menu",     description="✨ Главное меню"),
]


async def _root(_: web.Request) -> web.Response:
    return web.Response(text="series-bot is alive")


def _make_health_handler(session_factory: async_sessionmaker):
    async def _health(_: web.Request) -> web.Response:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as e:
            logging.warning("health: DB unreachable: %s", e)
            return web.Response(text="db down", status=503)
        return web.Response(text="ok")
    return _health


async def start_http_server(port: int, session_factory: async_sessionmaker) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _root)
    app.router.add_get("/health", _make_health_handler(session_factory))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("HTTP health server started on 0.0.0.0:%d", port)
    return runner


async def self_pinger(url: str, interval_s: int = 600) -> None:
    """Раз в interval_s пингует свой /health чтобы Render free не усыплял контейнер."""
    await asyncio.sleep(60)  # стартовая задержка чтобы бот успел подняться
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            try:
                r = await client.get(f"{url.rstrip('/')}/health")
                logging.info("self-ping status=%s", r.status_code)
            except Exception as e:
                logging.warning("self-ping failed: %s", e)
            await asyncio.sleep(interval_s)


def _init_sentry() -> None:
    """Sentry — opt-in через env SENTRY_DSN. Без DSN не активируется."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            send_default_pii=False,
            environment=os.getenv("RENDER_SERVICE_NAME") or os.getenv("ENV") or "prod",
        )
        logging.info("Sentry initialized")
    except Exception as e:
        logging.warning("Sentry init failed: %s", e)


def _setup_owner_alert(dp: Dispatcher, bot: Bot) -> None:
    """Если задан OWNER_TG_ID — при unhandled exception в handler'е
    шлём владельцу сообщение со stacktrace.
    """
    owner_raw = os.getenv("OWNER_TG_ID", "").strip()
    if not owner_raw or not owner_raw.isdigit():
        return
    owner_id = int(owner_raw)

    @dp.errors()
    async def _on_error(event) -> bool:
        logging.exception("Unhandled error: %s", event.exception)
        import traceback
        tb = "".join(traceback.format_exception(type(event.exception), event.exception, event.exception.__traceback__))
        # Telegram message max 4096 — обрезаем
        if len(tb) > 3500:
            tb = tb[:3500] + "\n…(обрезано)"
        try:
            await bot.send_message(
                owner_id,
                f"🚨 <b>Ошибка в боте</b>\n<pre>{tb}</pre>",
                parse_mode="HTML",
            )
        except Exception as e:
            logging.warning("Owner alert failed: %s", e)
        return True  # помечаем как обработанную, чтобы aiogram не валил выше

    logging.info("Owner alert wired for TG id %s", owner_id)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    _init_sentry()
    settings = Settings.from_env()
    logging.info(
        "Starting bot. DB: %s, API: %s, Groq: %s",
        settings.db_url.split('@')[-1] if '@' in settings.db_url else settings.db_url,
        settings.kp_api_base, "yes" if settings.groq_api_key else "no",
    )

    engine = make_engine(settings.db_url)
    await init_db(engine)
    session_factory = make_session_factory(engine)

    kp = KinopoiskClient(settings.kp_api_key, base_url=settings.kp_api_base)
    groq = GroqClient(settings.groq_api_key, model=settings.groq_model) if settings.groq_api_key else None

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(start_handlers.make_router(session_factory))
    dp.include_router(series_handlers.make_router(session_factory, kp, settings, groq))
    _setup_owner_alert(dp, bot)

    port = int(os.getenv("PORT", "8080"))
    http_runner = await start_http_server(port, session_factory)
    scheduler = start_scheduler(bot, session_factory, kp=kp)

    # Self-ping чтобы Render free не усыплял контейнер (раз в 10 мин)
    external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("EXTERNAL_URL")
    ping_task = None
    if external_url:
        ping_task = asyncio.create_task(self_pinger(external_url))
        logging.info("Self-pinger started for %s", external_url)
    else:
        logging.info("RENDER_EXTERNAL_URL not set — self-pinger disabled")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands(BOT_COMMANDS)
        logging.info("Bot commands set: %d", len(BOT_COMMANDS))
        # Описание бота — то что юзер видит ДО /start (в превью)
        try:
            await bot.set_my_short_description(short_description="Семейный учёт сериалов — для вас двоих 🛋️")
            await bot.set_my_description(description=(
                "🛋️ Диванные критики — семейный учёт сериалов.\n\n"
                "• Просто пиши название (или кидай постер/голосовое) — добавлю в очередь\n"
                "• Лайки и заметки\n"
                "• /match — что лайкнули оба\n"
                "• /suggest — подбор от ИИ\n"
                "• /swipe — Tinder для сериалов\n"
                "• Воскресенье 22:00 — спрошу про активные"
            ))
        except Exception as e:
            logging.warning("set_my_description failed: %s", e)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if ping_task:
            ping_task.cancel()
        scheduler.shutdown(wait=False)
        await http_runner.cleanup()
        if groq:
            await groq.close()
        await kp.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
