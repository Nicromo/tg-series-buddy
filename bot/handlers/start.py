"""Регистрация, /start, /help, /pair, /menu, обработка кнопок главного меню."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import repository as repo
from ..db.models import Pair, Series
from ..keyboards.main_menu import main_menu


class JoinPairFSM(StatesGroup):
    waiting_code = State()

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
    "<b>📺 Сериалы (общий список с партнёром):</b>\n"
    "/add &lt;название&gt; — найти и добавить\n"
    "(или просто пришли скрин с постером — распознаю)\n"
    "/list — наш общий «хочу посмотреть» (💛 — любим оба)\n"
    "/watching — смотрим сейчас\n"
    "/watched — досмотрели\n"
    "/rewatch — хотим пересмотреть\n"
    "/find &lt;запрос&gt; — поиск в наших сериалах\n\n"
    "<b>✨ Подбор:</b>\n"
    "/today — что включить сегодня\n"
    "/random — случайный из очереди\n"
    "/suggest — 3 рекомендации от ИИ\n"
    "/swipe — игровой режим: Tinder для сериалов\n\n"
    "<b>👫 Пара:</b>\n"
    "/pair — связаться с партнёром / показать статус пары\n\n"
    "<b>📊 Прочее:</b>\n"
    "/stats — статистика\n"
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

    def _join_code_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➕ Ввести код партнёра", callback_data="pair:enter"),
        ]])

    @router.message(Command("pair"))
    async def cmd_pair(message: Message, state: FSMContext = None) -> None:
        parts = (message.text or "").split(maxsplit=1)
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session,
                tg_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )

            if len(parts) == 1:
                # /pair без аргумента
                if user.pair_id:
                    pair = await session.get(Pair, user.pair_id)
                    members = await repo.get_pair_members(session, user.pair_id)
                    code = pair.invite_code if pair else "(ошибка)"
                    # Авто-sync существующих want/watching между членами
                    created = await repo.sync_pair_series(session, user.pair_id)
                    await session.commit()

                    member_lines = []
                    for m in members:
                        marker = "👤" if m.id == user.id else "👥"
                        label = m.full_name or (f"@{m.username}" if m.username else f"id={m.id}")
                        member_lines.append(f"{marker} {label}")

                    text_lines = [
                        f"👫 <b>Вы в паре</b> ({len(members)} чел.)",
                        "",
                        *member_lines,
                        "",
                    ]
                    if created > 0:
                        text_lines.append(
                            f"🔄 Синхронизировал списки — у партнёра появилось "
                            f"<b>{created}</b> новых сериала(ов) в «хочу/смотрю»."
                        )
                        text_lines.append("")
                    text_lines.append(f"🔗 Код для приглашения ещё одного: <code>{code}</code>")
                    await message.answer(
                        "\n".join(text_lines),
                        parse_mode="HTML",
                    )
                    return

                # Юзер ещё не в паре — спрашиваем что он хочет
                await message.answer(
                    "👫 <b>Создать пару с партнёром</b>\n\n"
                    "Тебе нужно:\n"
                    "1️⃣ <b>Получить мой код</b> — отправить партнёру → он введёт его у себя\n"
                    "2️⃣ Или <b>Ввести код партнёра</b> — если он уже отправил свой\n\n"
                    "Что делаем?",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔗 Дай мой код", callback_data="pair:mycode")],
                        [InlineKeyboardButton(text="➕ Ввести код партнёра", callback_data="pair:enter")],
                    ]),
                )
                return

            # /pair <code>
            code = parts[1].strip()
            pair = await repo.join_pair_by_code(session, user, code)
            if pair is None:
                await message.answer("❌ Код не найден. Проверь правильность.")
                return
            # Sync существующих want/watching обоих
            created = await repo.sync_pair_series(session, pair.id)
            await session.commit()
            extra = f"\n🔄 Синхронизировал списки — у вас обоих появилось <b>{created}</b> новых сериала(ов)." if created else ""
            await message.answer(
                "✅ Готово, вы в одной паре!\n"
                "Теперь /match покажет ваши общие лайки 💛"
                + extra,
                parse_mode="HTML",
            )

    @router.callback_query(F.data == "pair:mycode")
    async def cb_pair_mycode(call: CallbackQuery) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            if user.pair_id:
                pair = await session.get(Pair, user.pair_id)
                code = pair.invite_code if pair else "(ошибка)"
            else:
                pair = await repo.create_pair_for_user(session, user)
                code = pair.invite_code
            await session.commit()
        await call.answer()
        await call.message.answer(
            f"🔗 Твой код: <code>{code}</code>\n\n"
            f"Перешли его партнёру одним сообщением. Они нажмут "
            f"<b>«👫 Пара» → «➕ Ввести код партнёра»</b> и введут этот код.",
            parse_mode="HTML",
        )

    @router.callback_query(F.data == "pair:enter")
    async def cb_pair_enter(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(JoinPairFSM.waiting_code)
        await call.answer()
        await call.message.answer(
            "✏️ Пришли код, который дал партнёр (или /cancel чтобы отменить):",
        )

    @router.message(JoinPairFSM.waiting_code, Command("cancel"))
    async def cb_pair_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(JoinPairFSM.waiting_code)
    async def cb_pair_code_received(message: Message, state: FSMContext) -> None:
        code = (message.text or "").strip()
        if not code or len(code) > 32:
            await message.answer("Странный код. Попробуй ещё раз или /cancel")
            return
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            pair = await repo.join_pair_by_code(session, user, code)
            if pair is None:
                await message.answer("❌ Код не найден. Проверь правильность или /cancel")
                return
            created = await repo.sync_pair_series(session, pair.id)
            await session.commit()
        await state.clear()
        extra = f"\n🔄 Синхронизировал списки — у вас обоих появилось <b>{created}</b> новых сериала(ов)." if created else ""
        await message.answer(
            "✅ Готово, вы в одной паре!\n"
            "Теперь /match покажет ваши общие лайки 💛"
            + extra,
            parse_mode="HTML",
        )

    # ---------- Кнопки главного меню (текстовые) → дёргают команды ----------

    @router.message(F.text == "ℹ️ Помощь")
    async def btn_help(message: Message) -> None:
        await cmd_help(message)

    @router.message(F.text == "👫 Пара")
    async def btn_pair(message: Message) -> None:
        # Эмулируем /pair без аргументов — выдаст invite-код или статус
        await cmd_pair(message)

    return router
