"""Регистрация, /start, /help, /pair, /menu, обработка кнопок главного меню."""

from __future__ import annotations

import os
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import repository as repo
from ..db.models import Pair, Series, YoutubeSubscription
from ..keyboards.main_menu import main_menu
from ..services.youtube_rss import (
    resolve_channel, fetch_latest_videos, fetch_channel_title_robust, is_youtube_short,
)


class JoinPairFSM(StatesGroup):
    waiting_code = State()


class DonateAmountFSM(StatesGroup):
    waiting = State()


class SubFSM(StatesGroup):
    waiting_url = State()

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
    "<b>📺 Сериалы и фильмы (общий с партнёром):</b>\n"
    "/add &lt;название&gt; — найти и добавить (можно несколько через запятую/перевод строки)\n"
    "Также — пришли постер фото 📸, голосовое 🎤 или просто напиши название\n"
    "/list — наш общий «хочу посмотреть»\n"
    "/watching — смотрим сейчас\n"
    "/watched — досмотрели\n"
    "/rewatch — хотим пересмотреть\n"
    "/find &lt;запрос&gt; — поиск в наших сериалах\n\n"
    "<b>✨ Подбор и обнаружение:</b>\n"
    "/today — сводка + что включить сегодня\n"
    "/random — случайный из очереди\n"
    "/suggest — подбор от ИИ (тип → жанр → год → 5 вариантов с «Дальше»)\n"
    "/swipe — Tinder для НОВЫХ сериалов под ваши жанры\n"
    "/upcoming — премьеры на этой неделе под ваши жанры\n"
    "/cinema — что в кино в твоём городе (с реальными сеансами)\n"
    "/top &lt;жанр&gt; — топ-10 КП по жанру\n"
    "/poll — голосуем с партнёром «что включим»\n"
    "/where [название] — где смотреть (списком или конкретный)\n\n"
    "<b>🚫 Фильтры ИИ:</b>\n"
    "/blacklist — жанры которые никогда не предлагать\n"
    "Под предложением: «✅ Уже смотрел», «❌ Не интересно» — больше не предложит\n\n"
    "<b>👫 Пара:</b>\n"
    "/pair — связаться с партнёром / показать статус пары\n\n"
    "<b>📺 YouTube подписки:</b>\n"
    "/sub [url или @handle] — подписаться на канал\n"
    "/subs — список подписок (общий с партнёром)\n\n"
    "<b>📊 Прочее:</b>\n"
    "/stats — статистика\n"
    "/checkin — спросить про активные сейчас\n"
    "/donate — поддержать бота ⭐ Telegram Stars\n"
    "/menu — показать меню снизу\n"
    "/help — эта справка\n\n"
    "<i>💬 Можно просто писать боту — он понимает свободные команды.</i>\n"
    "<i>Примеры: «исключи аниме», «трейлер Severance», «что в кино сегодня?», «посоветуй триллер 2020-х».</i>"
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

            # Deep link: /start show_<series_id> — открыть карточку
            # /start add_<kp_id> — сразу добавить (приходит из inline @-поиска)
            arg = (command.args or "").strip() if command else ""
            if arg.startswith("add_"):
                try:
                    add_kp_id = int(arg[len("add_"):])
                except Exception:
                    add_kp_id = 0
                if add_kp_id:
                    await message.answer(
                        "👋 Добавляю сериал, который ты выбрал в поиске…",
                        reply_markup=main_menu(),
                    )
                    # Используем helper из _series_helpers (через ленивый импорт)
                    from ._series_helpers import add_by_kp_id
                    from ..services.kinopoisk import KinopoiskClient
                    from ..config import Settings
                    settings = Settings.from_env()
                    kp_client = KinopoiskClient(settings.kp_api_key, base_url=settings.kp_api_base)
                    try:
                        await add_by_kp_id(
                            message.bot, message.chat.id, message.from_user.id,
                            add_kp_id, session_factory, kp_client,
                        )
                    finally:
                        await kp_client.close()
                    return
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

    async def _do_subscribe(message: Message, url_or_handle: str) -> None:
        """Подписка с валидацией URL и человеческими ошибками."""
        # Детект: пользователь дал URL ВИДЕО (не канала)
        if "youtube.com/watch" in url_or_handle.lower() or "youtu.be/" in url_or_handle.lower():
            await message.answer(
                "🤔 Это ссылка на <b>видео</b>, а мне нужен <b>канал</b>.\n"
                "Открой видео → жми на имя автора → скопируй ссылку с его страницы.\n"
                "Или пришли @handle, например <code>@kinopoisk</code>.",
                parse_mode="HTML",
            )
            return
        if "youtube.com/shorts/" in url_or_handle.lower():
            await message.answer(
                "🤔 Это ссылка на Shorts, а нужна на канал. "
                "Дай ссылку из адреса канала (с @ в URL) или его @handle.",
            )
            return

        await message.bot.send_chat_action(message.chat.id, action="typing")
        info = await resolve_channel(url_or_handle)
        if not info:
            await message.answer(
                "❌ Не нашёл такой канал.\n"
                "Принимаю: <code>@handle</code>, "
                "<code>https://www.youtube.com/@handle</code> или "
                "<code>https://www.youtube.com/channel/UCxxx</code>",
                parse_mode="HTML",
            )
            return
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

    @router.message(Command("sub"))
    async def cmd_sub(message: Message, state: FSMContext) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await state.set_state(SubFSM.waiting_url)
            await message.answer(
                "📺 <b>Подписка на YouTube-канал</b>\n\n"
                "Пришли ссылку или @handle одним сообщением. Примеры:\n"
                "<code>https://www.youtube.com/@MrBeast</code>\n"
                "<code>@kinopoisk</code>\n"
                "<code>https://www.youtube.com/channel/UCxxx</code>\n\n"
                "/cancel — отменить",
                parse_mode="HTML",
            )
            return
        await _do_subscribe(message, parts[1].strip())

    @router.message(SubFSM.waiting_url, Command("cancel"))
    async def sub_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(SubFSM.waiting_url)
    async def sub_url_typed(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _do_subscribe(message, (message.text or "").strip())

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

        # Чиним сломанные title через robust-fetch (RSS + HTML og:title)
        fixed = 0
        for s in subs:
            if _title_looks_broken(s.channel_title):
                try:
                    fresh = await fetch_channel_title_robust(s.channel_id)
                    if fresh and not _title_looks_broken(fresh):
                        async with session_factory() as session:
                            db_s = await session.get(YoutubeSubscription, s.id)
                            if db_s:
                                db_s.channel_title = fresh
                                await session.commit()
                                s.channel_title = fresh
                                fixed += 1
                except Exception as e:
                    import logging
                    logging.warning("YT auto-fix title %s failed: %s", s.channel_id, e)

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

    # ---------- Поддержать проект (Telegram Stars) ----------

    _STAR_OPTIONS = [1, 10, 50, 100, 500]  # быстрые суммы

    @router.message(Command("donate"))
    async def cmd_donate(message: Message) -> None:
        bot_user = await message.bot.me()
        share_link = f"https://t.me/{bot_user.username}"
        # Кнопки сумм — по 5 в ряд если влезут, иначе по 2-3
        amount_row = [
            InlineKeyboardButton(text=f"⭐ {n}", callback_data=f"donate:{n}")
            for n in _STAR_OPTIONS
        ]
        rows = [
            amount_row,
            [InlineKeyboardButton(text="✏️ Своя сумма", callback_data="donate:custom")],
            [InlineKeyboardButton(text="📤 Рассказать о боте друзьям", url=f"https://t.me/share/url?url={share_link}&text=Бот для семейного учёта сериалов")],
        ]
        cloudtips = os.getenv("DONATE_CLOUDTIPS_URL", "").strip()
        if cloudtips:
            rows.append([InlineKeyboardButton(text="🔗 CloudTips (с карты)", url=cloudtips)])

        await message.answer(
            "💛 <b>Спасибо что пользуешься ботом!</b>\n\n"
            "Бот бесплатный — у меня нет на нём заработка. Если хочется "
            "помочь оплатить хостинг и API — вот варианты "
            "<i>без раскрытия каких-либо реквизитов:</i>\n\n"
            "⭐ <b>Звёздами Telegram</b> — самый простой, всё внутри Telegram, "
            "ничего не привязывать. От <b>1 ⭐</b> и выше.\n"
            "📤 <b>Поделись с друзьями</b> — самая ценная помощь.\n\n"
            "Выбери сумму или введи свою:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.message(F.text == "💛 Поддержать")
    async def btn_donate(message: Message) -> None:
        await cmd_donate(message)

    async def _send_stars_invoice(bot, chat_id: int, user_id: int, amount: int) -> None:
        """Создаёт счёт в Telegram Stars (валюта XTR)."""
        if amount < 1:
            amount = 1
        if amount > 100000:
            amount = 100000
        try:
            await bot.send_invoice(
                chat_id=chat_id,
                title=f"⭐ Поддержать бота ({amount} {'звезда' if amount == 1 else 'звёзд'})",
                description=(
                    "Спасибо что помогаешь! Деньги пойдут на хостинг "
                    "(Render + Neon Postgres) и API."
                ),
                payload=f"donate_{amount}_{user_id}",
                currency="XTR",  # Telegram Stars
                prices=[LabeledPrice(label=f"{amount} ⭐", amount=amount)],
            )
        except Exception as e:
            import logging
            logging.exception("send_invoice failed")
            await bot.send_message(chat_id, f"😕 Не получилось создать счёт: {e}")

    @router.callback_query(F.data.startswith("donate:"))
    async def cb_donate_stars(call: CallbackQuery, state: FSMContext) -> None:
        arg = call.data.split(":")[1]
        if arg == "custom":
            await state.set_state(DonateAmountFSM.waiting)
            await call.answer()
            await call.message.answer(
                "✏️ Введи сколько звёзд хочешь подарить (от <b>1</b> до 100000).\n"
                "/cancel — отменить",
                parse_mode="HTML",
            )
            return
        try:
            amount = int(arg)
        except Exception:
            await call.answer("Странная сумма")
            return
        await call.answer()
        await _send_stars_invoice(call.bot, call.message.chat.id, call.from_user.id, amount)

    @router.message(DonateAmountFSM.waiting, Command("cancel"))
    async def donate_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(DonateAmountFSM.waiting)
    async def donate_amount_typed(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text.isdigit():
            await message.answer("Нужно число от 1 до 100000. Попробуй ещё или /cancel")
            return
        amount = int(text)
        if amount < 1 or amount > 100000:
            await message.answer("Число от 1 до 100000. /cancel — отменить")
            return
        await state.clear()
        await _send_stars_invoice(message.bot, message.chat.id, message.from_user.id, amount)

    @router.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery) -> None:
        # Просто подтверждаем — для Stars никаких проверок не нужно
        await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def on_successful_payment(message: Message) -> None:
        amount = message.successful_payment.total_amount
        await message.answer(
            f"🌟 <b>Спасибо за поддержку!</b>\n\n"
            f"Получил <b>{amount}</b> ⭐ — это правда помогает держать бота онлайн. "
            f"Хорошего тебе вечера за просмотром 🍿",
            parse_mode="HTML",
        )

    return router
