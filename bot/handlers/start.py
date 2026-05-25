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
from ..db.models import Pair, Series, YoutubeSubscription
from ..keyboards.main_menu import main_menu
from ..services.youtube_rss import resolve_channel, fetch_latest_videos


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
    "/swipe — Tinder для новых сериалов\n"
    "/upcoming — премьеры на этой неделе под ваши жанры\n"
    "/top &lt;жанр&gt; — топ-10 КП по жанру (драма, триллер, ...)\n"
    "/poll — голосуем с партнёром «что включить»\n"
    "/where — где смотреть из активных списков\n\n"
    "<b>👫 Пара:</b>\n"
    "/pair — связаться с партнёром / показать статус пары\n\n"
    "<b>📺 YouTube подписки:</b>\n"
    "/sub &lt;url или @handle&gt; — подписаться на канал\n"
    "/subs — список подписок (общий с партнёром)\n\n"
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

    # ---------- YouTube подписки ----------

    @router.message(Command("sub"))
    async def cmd_sub(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "📺 <b>Подписка на YouTube-канал</b>\n\n"
                "Пришли ссылку или @handle:\n"
                "<code>/sub https://www.youtube.com/@MrBeast</code>\n"
                "<code>/sub @kinopoisk</code>\n"
                "<code>/sub https://www.youtube.com/channel/UCxxx</code>\n\n"
                "Бот будет пушить новые видео.\n"
                "Список подписок: /subs",
                parse_mode="HTML",
            )
            return
        url_or_handle = parts[1].strip()
        await message.bot.send_chat_action(message.chat.id, action="typing")
        info = await resolve_channel(url_or_handle)
        if not info:
            await message.answer(
                "❌ Не нашёл такой канал. Проверь ссылку или попробуй /sub @handle",
            )
            return
        # Помечаем последнее видео сразу — чтобы при подписке не получить
        # «вышло X видео» пачкой за день назад
        latest = await fetch_latest_videos(info.channel_id, limit=1)
        last_vid_id = latest[0].video_id if latest else None
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            sub, created = await repo.add_youtube_subscription(
                session, user, info.channel_id, info.title,
            )
            if created and last_vid_id:
                await repo.mark_youtube_video_sent(session, sub.id, last_vid_id)
            await session.commit()
        scope = "вам с партнёром" if user.pair_id else "тебе"
        if created:
            await message.answer(
                f"✅ Подписался на <b>{info.title}</b>.\n"
                f"Буду присылать новые видео {scope}. /subs — список.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"💡 <b>{info.title}</b> уже в подписках. /subs — список.",
                parse_mode="HTML",
            )

    def _title_looks_broken(t: str) -> bool:
        """Эвристика на сломанный UTF-8: «Ð», «Ñ» подряд — это типичная
        mojibake от `.encode().decode('unicode_escape')` ошибки."""
        if not t:
            return True
        # Если в строке нет ни одного нормального буквенно-цифрового символа,
        # либо >50% — символы из «mojibake» диапазона
        bad = sum(1 for ch in t if ch in "ÐÑ°±²³´µ¶·¸¹º»¼½¾¿ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÒÓÔÕÖ×ØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõö")
        return bad >= 3 and bad / max(1, len(t)) > 0.4

    @router.message(Command("subs"))
    async def cmd_subs(message: Message) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            subs = await repo.list_youtube_subscriptions(session, user)
        if not subs:
            await message.answer(
                "📺 Подписок пока нет.\n"
                "Добавь канал: <code>/sub @handle</code> или /sub &lt;url&gt;",
                parse_mode="HTML",
            )
            return

        # Чиним сломанные title — дёргаем RSS feed (один запрос на канал)
        fixed = 0
        for s in subs:
            if _title_looks_broken(s.channel_title):
                try:
                    from ..services.youtube_rss import _fetch_channel_title
                    fresh = await _fetch_channel_title(s.channel_id)
                    if fresh and not _title_looks_broken(fresh):
                        async with session_factory() as session:
                            db_s = await session.get(YoutubeSubscription, s.id)
                            if db_s:
                                db_s.channel_title = fresh
                                await session.commit()
                                s.channel_title = fresh
                                fixed += 1
                except Exception:
                    pass

        lines = [f"📺 <b>YouTube подписки ({len(subs)}):</b>", ""]
        rows = []
        for i, s in enumerate(subs, 1):
            lines.append(
                f"{i}. <a href=\"https://www.youtube.com/channel/{s.channel_id}\">"
                f"{s.channel_title}</a>"
            )
            rows.append([InlineKeyboardButton(text=f"❌ {i}. Отписаться от {s.channel_title[:25]}", callback_data=f"ytunsub:{s.id}")])
        lines.append("")
        if fixed:
            lines.append(f"<i>🔧 Подчинил {fixed} названий из старых подписок</i>")
        lines.append("<i>Жми ❌ чтобы отписаться</i>")
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data.startswith("ytunsub:"))
    async def cb_yt_unsub(call: CallbackQuery) -> None:
        sub_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            sub = await session.get(YoutubeSubscription, sub_id)
            title = sub.channel_title if sub else "канал"
            removed = await repo.remove_youtube_subscription(session, sub_id)
            await session.commit()
        if removed:
            await call.answer(f"❌ Отписался")
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await call.message.answer(f"❌ Отписался от <b>{title}</b>", parse_mode="HTML")
        else:
            await call.answer("Уже не было")

    # ---------- Кнопки главного меню (текстовые) → дёргают команды ----------

    @router.message(F.text == "ℹ️ Помощь")
    async def btn_help(message: Message) -> None:
        await cmd_help(message)

    @router.message(F.text == "👫 Пара")
    async def btn_pair(message: Message) -> None:
        # Эмулируем /pair без аргументов — выдаст invite-код или статус
        await cmd_pair(message)

    return router
