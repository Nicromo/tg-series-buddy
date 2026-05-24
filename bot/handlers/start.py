"""Регистрация, /start, /help, /pair, /menu, обработка кнопок главного меню."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import repository as repo
from ..db.models import Pair, Series
from ..keyboards.main_menu import main_menu

WELCOME = (
    "🛋️ <b>Диванные критики</b>\n"
    "<i>Семейный учёт сериалов для вас двоих</i>\n\n"
    "🎬 Добавь сериал — кинь название, или просто перешли скрин/постер, "
    "я сам распознаю.\n"
    "💛 Ставь лайки — когда оба лайкнули, /match покажет пары совпадений.\n"
    "🔁 Досмотрели — оценишь, и сериал уйдёт в архив. Захочешь "
    "пересмотреть — жми 🔁.\n\n"
    "📅 Каждое воскресенье в 22:00 спрашиваю как у тебя дела с активными "
    "сериалами — отметишь одной кнопкой.\n\n"
    "👇 Используй меню снизу или команды:"
)

HELP_TEXT = (
    "🛋️ <b>Диванные критики</b>\n\n"
    "<b>📺 Сериалы:</b>\n"
    "/add &lt;название&gt; — найти и добавить\n"
    "(или просто пришли скрин с постером — распознаю)\n"
    "/list — что хотим посмотреть\n"
    "/watching — что смотрим сейчас\n"
    "/watched — досмотрели\n"
    "/rewatch — хотим пересмотреть\n"
    "/find &lt;запрос&gt; — поиск в твоих сериалах\n\n"
    "<b>✨ Подбор:</b>\n"
    "/today — что включить сегодня\n"
    "/random — случайный из очереди\n"
    "/suggest — 3 рекомендации от ИИ\n"
    "/swipe — игровой режим: Tinder для сериалов\n"
    "/match — что лайкнули вы оба\n\n"
    "<b>👫 Пара:</b>\n"
    "/pair — инвайт-код для партнёра\n"
    "/pair &lt;код&gt; — присоединиться\n\n"
    "<b>📊 Прочее:</b>\n"
    "/stats — статистика пары\n"
    "/checkin — спросить про активные сейчас\n"
    "/menu — показать меню снизу\n"
    "/help — эта справка"
)


def make_router(session_factory: async_sessionmaker) -> Router:
    router = Router(name="start")

    @router.message(CommandStart())
    async def cmd_start(message: Message, command: CommandObject = None) -> None:
        async with session_factory() as session:
            await repo.get_or_create_user(
                session,
                tg_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )
            await session.commit()

            # Deep link: /start show_<series_id> — открыть карточку конкретного сериала
            arg = (command.args or "").strip() if command else ""
            if arg.startswith("show_"):
                try:
                    series_id = int(arg[len("show_"):])
                    series = await session.get(Series, series_id)
                except Exception:
                    series = None
                if series:
                    await message.answer(
                        f"\U0001f3ac \u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 \u043e\u0442 \u043f\u0430\u0440\u0442\u043d\u0451\u0440\u0430:",
                        reply_markup=main_menu(),
                    )
                    # Render card directly here (without /add flow). Simple: just message with poster.
                    caption_lines = [f"\U0001f3ac <b>{series.title_ru}</b>"]
                    if series.year:
                        caption_lines[0] += f" ({series.year})"
                    if series.rating_kp:
                        caption_lines.append(f"\u2b50 \u041a\u041f {series.rating_kp:.1f}")
                    if series.description_ru:
                        d = series.description_ru[:400]
                        caption_lines.append("")
                        caption_lines.append(d + ("\u2026" if len(series.description_ru) > 400 else ""))
                    cap = "\n".join(caption_lines)
                    cap += f"\n\n\U0001f4ac \u0427\u0442\u043e\u0431\u044b \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u0435\u0431\u0435 \u2014 \u043d\u0430\u043f\u0438\u0448\u0438: <code>/add {series.title_ru}</code>"
                    if series.poster_url:
                        try:
                            await message.bot.send_photo(message.chat.id, photo=series.poster_url, caption=cap, parse_mode="HTML")
                            return
                        except Exception:
                            pass
                    await message.answer(cap, parse_mode="HTML")
                    return

        # Маленький "хлоп" перед приветствием — Telegram анимирует одиночный эмоджи
        try:
            await message.answer("🍿🎬")
        except Exception:
            pass
        await message.answer(WELCOME, parse_mode="HTML", reply_markup=main_menu())
        await message.answer(HELP_TEXT, parse_mode="HTML")

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=main_menu())

    @router.message(Command("menu"))
    async def cmd_menu(message: Message) -> None:
        await message.answer("Меню обновлено 👇", reply_markup=main_menu())

    @router.message(Command("pair"))
    async def cmd_pair(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session,
                tg_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )

            if len(parts) == 1:
                if user.pair_id:
                    pair = await session.get(Pair, user.pair_id)
                    code = pair.invite_code if pair else "(ошибка)"
                else:
                    pair = await repo.create_pair_for_user(session, user)
                    code = pair.invite_code
                await session.commit()
                await message.answer(
                    f"🔗 Твой инвайт-код: <code>{code}</code>\n\n"
                    f"Перешли его жене/партнёру. Они напишут:\n"
                    f"<code>/pair {code}</code>",
                    parse_mode="HTML",
                )
                return

            code = parts[1].strip()
            pair = await repo.join_pair_by_code(session, user, code)
            if pair is None:
                await message.answer("❌ Код не найден. Проверь правильность.")
                return
            await session.commit()
            await message.answer(
                "✅ Готово, вы в одной паре!\n"
                "Теперь /match покажет ваши общие лайки 💛"
            )

    # ---------- Кнопки главного меню (текстовые) → дёргают команды ----------

    @router.message(F.text == "ℹ️ Помощь")
    async def btn_help(message: Message) -> None:
        await cmd_help(message)

    return router
