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
    BotCommand(command="list",     description="📋 Наш общий список"),
    BotCommand(command="suggest",  description="🪄 Подбор от ИИ"),
    BotCommand(command="trending", description="🔥 Громкие новинки"),
    BotCommand(command="cinema",   description="🎫 Что в кино"),
    BotCommand(command="profile",  description="👤 Профиль"),
    BotCommand(command="menu",     description="📂 Все возможности"),
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


_ERROR_HINTS: dict[str, str] = {
    # Telegram API errors — частые причины + что чинить
    "BUTTON_DATA_INVALID":
        "callback_data > 64 байт или не-ASCII. Сократи slug или замени кириллицу.",
    "MESSAGE_NOT_MODIFIED":
        "edit_text с тем же текстом+разметкой. Игнорируй или меняй контент.",
    "MESSAGE_TO_EDIT_NOT_FOUND":
        "Сообщение уже удалено или старше 48 ч. Шли новое вместо edit.",
    "MESSAGE_TO_DELETE_NOT_FOUND":
        "Сообщение уже удалено. Оберни delete_message в try/except.",
    "MESSAGE_CANT_BE_DELETED":
        "Сообщение нельзя удалить (старше 48 ч / не от бота).",
    "QUERY_ID_INVALID":
        "callback_query устарел (>15 сек). Отвечай быстрее или игнорируй.",
    "WEBHOOK_REQUIRE_HTTPS":
        "Webhook URL должен быть https.",
    "Bad Request: can't parse entities":
        "HTML/Markdown сломан — спецсимволы в тексте. html.escape() поможет.",
    "Bad Request: chat not found":
        "Юзер удалил чат или заблокировал бота.",
    "Bad Request: message is too long":
        "Сообщение > 4096 символов. Режь на части.",
    "Forbidden: bot was blocked by the user":
        "Юзер заблокировал бота. Можно пометить в БД и не слать.",
    "Forbidden: bot is not a member":
        "Бот не в чате/канале. Пригласи или сними команду.",
    "Too Many Requests":
        "Rate limit Telegram. Жди retry_after сек.",
    # SQLAlchemy / DB
    "asyncpg.exceptions.UndefinedColumnError":
        "Колонка в БД отсутствует. Проверь миграции в repository.init_db().",
    "asyncpg.exceptions.UniqueViolationError":
        "Дубликат уникального ключа. Проверь логику upsert.",
    "asyncpg.exceptions.ConnectionDoesNotExistError":
        "Соединение с Neon отвалилось. Pool вернёт новое.",
    # HTTP
    "httpx.ConnectError":
        "Не достучаться до апстрима (KP / Groq / YouTube). Проверь сеть и URL.",
    "httpx.ReadTimeout":
        "Апстрим не ответил вовремя. Увеличь timeout или ретрай.",
    "httpx.HTTPStatusError":
        "Апстрим вернул 4xx/5xx — проверь параметры запроса.",
    # Python typical
    "AttributeError: 'NoneType'":
        "Где-то None вместо объекта. Добавь проверку или .get().",
    "KeyError":
        "Ключ отсутствует в dict. Используй .get() с дефолтом.",
    "IndexError":
        "Индекс за пределами списка. Проверь длину перед обращением.",
    "TypeError":
        "Несовместимые типы. Проверь сигнатуру функции.",
    "ValueError":
        "Невалидное значение. Проверь конвертацию (int/float/split).",
}


def _humanize_error(exc: BaseException) -> tuple[str, str | None]:
    """Возвращает (короткая суть, подсказка-причина) для типичных ошибок."""
    exc_name = type(exc).__name__
    msg = str(exc)
    full = f"{exc_name}: {msg}"
    # 1. Точные совпадения по подстрокам
    for needle, hint in _ERROR_HINTS.items():
        if needle in full:
            return f"{exc_name}: {msg.splitlines()[0][:200]}", hint
    return f"{exc_name}: {msg.splitlines()[0][:200] if msg else '(без сообщения)'}", None


def _describe_update(update) -> str:
    """Короткая шапка: какой апдейт, от кого, в каком чате, что внутри."""
    if update is None:
        return "—"
    parts: list[str] = []
    try:
        if getattr(update, "message", None):
            m = update.message
            who = m.from_user.username or m.from_user.full_name if m.from_user else "?"
            text = (m.text or m.caption or f"[{m.content_type}]")[:80]
            parts.append(f"message от @{who} (chat={m.chat.id}): {text!r}")
        elif getattr(update, "callback_query", None):
            cb = update.callback_query
            who = cb.from_user.username or cb.from_user.full_name if cb.from_user else "?"
            parts.append(f"callback от @{who}: data={cb.data!r}")
        elif getattr(update, "pre_checkout_query", None):
            parts.append("pre_checkout_query")
        else:
            parts.append(f"update_id={update.update_id}")
    except Exception:
        parts.append("(не удалось распарсить update)")
    return " | ".join(parts)


def _setup_owner_alert(dp: Dispatcher, bot: Bot) -> None:
    """Если задан OWNER_TG_ID — при unhandled exception в handler'е
    шлём владельцу человекочитаемое сообщение с сутью + контекстом + traceback.
    """
    owner_raw = os.getenv("OWNER_TG_ID", "").strip()
    if not owner_raw or not owner_raw.isdigit():
        return
    owner_id = int(owner_raw)

    @dp.errors()
    async def _on_error(event) -> bool:
        import html
        import traceback as tb_mod

        exc = event.exception
        update = getattr(event, "update", None)
        logging.exception("Unhandled error: %s", exc)

        summary, hint = _humanize_error(exc)
        ctx = _describe_update(update)

        # Где в нашем коде упало (последний кадр из bot/)
        tb_frames = tb_mod.extract_tb(exc.__traceback__)
        our_frames = [f for f in tb_frames if "/bot/" in f.filename or "\\bot\\" in f.filename]
        last = our_frames[-1] if our_frames else (tb_frames[-1] if tb_frames else None)
        where = ""
        if last:
            short_file = last.filename.split("/bot/")[-1].split("\\bot\\")[-1]
            where = f"📍 <b>bot/{html.escape(short_file)}:{last.lineno}</b> in <code>{html.escape(last.name)}()</code>\n"

        # Полный traceback (для копания) — компактно, обрезаем по 3000 символов
        full_tb = "".join(tb_mod.format_exception(type(exc), exc, exc.__traceback__))
        if len(full_tb) > 2800:
            # Оставляем первые 700 + последние 2100 — там обычно суть
            full_tb = full_tb[:700] + "\n…(середина обрезана)…\n" + full_tb[-2100:]

        hint_block = f"💡 <b>Причина:</b> {html.escape(hint)}\n" if hint else ""

        text = (
            "🚨 <b>Ошибка в боте</b>\n\n"
            f"<b>Что:</b> <code>{html.escape(summary)}</code>\n"
            f"{where}"
            f"<b>Контекст:</b> {html.escape(ctx)}\n"
            f"{hint_block}"
            f"\n<b>Traceback:</b>\n<pre>{html.escape(full_tb)}</pre>"
        )
        # Telegram message max 4096
        if len(text) > 4000:
            text = text[:3950] + "\n…(обрезано)</pre>"

        try:
            await bot.send_message(owner_id, text, parse_mode="HTML")
        except Exception as e:
            logging.warning("Owner alert failed: %s", e)
            # Фоллбек — без HTML, plain
            try:
                plain = f"🚨 Ошибка: {summary}\nГде: {where}\nКонтекст: {ctx}"
                await bot.send_message(owner_id, plain[:4000])
            except Exception:
                pass
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
