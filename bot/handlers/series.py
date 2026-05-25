"""Хендлеры команд, callback-кнопок, кнопок главного меню и фото."""

from __future__ import annotations

import io
import logging
import random
import re
from collections import Counter
from typing import Optional


def _parse_titles_bulk(text: str) -> list[str]:
    """Разбивает массивный ввод на список названий.

    Поддерживает:
    - Каждое название с новой строки (с/без номеров «1.», «- », «• »)
    - Через запятую или точку с запятой
    - Смешанно

    Возвращает [] если ввод выглядит как одно название (без явных
    разделителей или с короткими частями).
    """
    # 1) По переводам строки — самый надёжный признак списка
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) >= 2:
        cleaned: list[str] = []
        for l in lines:
            # «1. X», «1) X», «2: X» → X
            l = re.sub(r"^\d+\s*[\.\)\:]\s*", "", l)
            # «- X», «• X», «* X», «– X»
            l = re.sub(r"^[\-•*–]\s*", "", l)
            l = l.strip()
            if len(l) >= 2:
                cleaned.append(l)
        if len(cleaned) >= 2:
            return cleaned

    # 2) Через запятую / точку с запятой — только если все части осмысленные
    parts = re.split(r"[,;]+", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2 and all(len(p) >= 3 for p in parts):
        return parts

    return []

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputTextMessageContent,
    Message,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from ..db import repository as repo
from ..db.models import Series, UserSeries
from ..keyboards.main_menu import main_menu
from ..keyboards.series_kb import (
    bulk_move_keyboard,
    card_keyboard,
    checkin_keyboard,
    search_results_keyboard,
    swipe_keyboard,
    trailer_link_keyboard,
)
from ..services.groq_ai import GroqClient
from ..services.kinopoisk import KinopoiskClient, KPDetails
from ..services.loading import start_loading, stop_loading, with_loader
from ..services.wallpaper import build_week_wallpaper
from ..services.scheduler import run_weekly_checkin
from ..services.trailer import find_trailer_tg_link
from ..services.trailer_search import TrailerFinder, build_youtube_search_url
from ._series_helpers import (
    DIGIT_EMOJI,
    NoteFSM,
    PickFSM,
    RATING_LABELS,
    STATUS_LABELS,
    SwipeFSM,
    add_by_kp_id as _add_by_kp_id,
    details_to_series_dict as _details_to_series_dict,
    format_caption as _format_caption,
    is_unreleased,
    send_card as _send_card,
    send_suggestions_gallery,
)

logger = logging.getLogger(__name__)


def make_router(
    session_factory: async_sessionmaker,
    kp: KinopoiskClient,
    settings: Settings,
    groq: Optional[GroqClient] = None,
) -> Router:
    router = Router(name="series")
    trailer_finder = TrailerFinder()  # читает TMDB_API_KEY/YOUTUBE_API_KEY из env

    # ============== /add and КNopкa "🎬 Добавить" ==============
    @router.message(Command("add"))
    async def cmd_add(message: Message, state: FSMContext) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "📝 Напиши название сериала после команды:\n"
                "<code>/add Severance</code>\n\n"
                "Можно сразу несколько — каждое с новой строки или через запятую.\n"
                "Или пришли скрин/постер — распознаю 📸",
                parse_mode="HTML",
            )
            return
        query = parts[1].strip()
        # Несколько названий через \n или запятую → bulk
        bulk = _parse_titles_bulk(query)
        if bulk:
            await _bulk_add_titles(message.bot, message.chat.id, message.from_user.id, bulk)
            return
        await _do_search_and_show(message.bot, message.chat.id, message.from_user.id, query, state=state)

    @router.message(F.text == "🎬 Добавить")
    async def btn_add(message: Message) -> None:
        await message.answer(
            "🎬 <b>Добавить сериал</b>\n\n"
            "Напиши название после команды <code>/add Severance</code>\n"
            "Или просто пришли скрин с постером — распознаю 📸",
            parse_mode="HTML",
        )


    # ============== Voice сообщение → Whisper → поиск ==============
    @router.message(F.voice)
    async def on_voice(message: Message, state: FSMContext) -> None:
        if not groq:
            await message.answer("🎤 Голосовой поиск недоступен — нет GROQ_API_KEY")
            return
        await message.bot.send_chat_action(message.chat.id, action="typing")
        try:
            buf = io.BytesIO()
            await message.bot.download(message.voice, destination=buf)
            buf.seek(0)
            text = await groq.transcribe_voice(buf.read())
        except Exception as e:
            logger.exception("Voice transcribe crashed")
            await message.answer(f"😕 Не получилось распознать: {e}")
            return
        if not text:
            await message.answer("🤔 Не расслышал. Попробуй ещё раз или напиши текстом.")
            return
        await message.answer(f"🎤 Услышал: <b>{text}</b>\nИщу…", parse_mode="HTML")
        await _do_search_and_show(message.bot, message.chat.id, message.from_user.id, text, state=state)

    # ============== Photo → распознавание через Groq vision ==============
    @router.message(F.photo)
    async def on_photo(message: Message, state: FSMContext) -> None:
        if not groq:
            await message.answer(
                "📸 Распознавание скринов недоступно — не задан GROQ_API_KEY.\n"
                "Пока напиши название текстом: /add Severance"
            )
            return
        await message.bot.send_chat_action(message.chat.id, action="typing")
        try:
            # Берём самое большое разрешение
            biggest = message.photo[-1]
            buf = io.BytesIO()
            await message.bot.download(biggest, destination=buf)
            buf.seek(0)
            title = await groq.vision_recognize_series(buf.read())
        except Exception as e:
            logger.exception("Vision recognize crashed")
            await message.answer(f"😕 Не получилось обработать картинку: {e}")
            return

        if not title:
            await message.answer(
                "🤔 На картинке не вижу название сериала. "
                "Пришли постер поярче или напиши вручную: <code>/add название</code>",
                parse_mode="HTML",
            )
            return

        await message.answer(
            f"🔎 Распознал: <b>{title}</b>\nИщу в Кинопоиске… (подтверди что это оно — постер мог сбить)",
            parse_mode="HTML",
        )
        await _do_search_and_show(
            message.bot, message.chat.id, message.from_user.id, title,
            state=state, force_picker=True,
        )

    async def _bulk_add_titles(bot: Bot, chat_id: int, tg_user_id: int, titles: list[str]) -> None:
        """Массовое добавление списка названий. Шлёт один сводный отчёт."""
        await bot.send_message(
            chat_id,
            f"📚 Обрабатываю <b>{len(titles)}</b> названий…",
            parse_mode="HTML",
        )
        await bot.send_chat_action(chat_id, action="typing")

        added: list[str] = []
        already: list[str] = []
        not_found: list[str] = []

        for title in titles[:20]:  # cap = 20, чтобы не повесить бота
            try:
                hits = await kp.search(title, limit=1)
            except Exception as e:
                logger.warning("bulk: kp.search failed for %r: %s", title, e)
                not_found.append(title)
                continue
            if not hits:
                not_found.append(title)
                continue
            series, was_new = await _add_by_kp_id(
                bot, chat_id, tg_user_id, hits[0].kp_id, session_factory, kp, silent=True,
            )
            if series is None:
                not_found.append(title)
            elif was_new:
                added.append(series.title_ru)
            else:
                already.append(series.title_ru)

        # Сводный отчёт
        parts: list[str] = []
        if added:
            lst = "\n".join(f"  • {t}" for t in added)
            parts.append(f"✅ <b>Добавил в «👀 Хочу» ({len(added)}):</b>\n{lst}")
        if already:
            lst = "\n".join(f"  • {t}" for t in already)
            parts.append(f"💡 <b>Уже было ({len(already)}):</b>\n{lst}")
        if not_found:
            lst = "\n".join(f"  • {t}" for t in not_found)
            parts.append(f"🤷 <b>Не нашёл ({len(not_found)}):</b>\n{lst}")
        if len(titles) > 20:
            parts.append(f"<i>… остальные {len(titles) - 20} не обработаны, повтори запрос частями.</i>")

        await bot.send_message(
            chat_id, "\n\n".join(parts) or "Ничего не добавил 🤷",
            parse_mode="HTML",
        )

    async def _do_search_and_show(
        bot: Bot, chat_id: int, tg_user_id: int, query: str, *,
        state: Optional[FSMContext] = None,
        force_picker: bool = False,
    ) -> None:
        await bot.send_chat_action(chat_id, action="typing")
        try:
            hits = await kp.search(query, limit=5)
        except Exception as e:
            logger.exception("KP search failed")
            await bot.send_message(chat_id, f"😕 Не получилось найти: {e}")
            return

        # Проверим релевантность: содержит ли хотя бы один результат запрос как substring
        def _looks_relevant(hits_list, q: str) -> bool:
            if not hits_list:
                return False
            q_low = q.lower().strip()
            # Берём первые 3 слова запроса (если они длинные)
            words = [w for w in q_low.split() if len(w) >= 3]
            if not words:
                # Запрос короткий — проверяем что title начинается похоже
                for h in hits_list[:3]:
                    if h.title_ru and h.title_ru.lower().startswith(q_low[:4]):
                        return True
                return False
            # Хотя бы один hit должен содержать хоть одно слово запроса
            for h in hits_list[:5]:
                blob = (h.title_ru + " " + (h.title_en or "")).lower()
                if any(w in blob for w in words):
                    return True
            return False

        # Если поиск ничего не нашёл или нашёл мусор — пробуем нормализовать через Groq
        if (not hits or not _looks_relevant(hits, query)) and groq is not None:
            fixed = await groq.fix_query(query)
            if fixed and fixed.lower() != query.lower():
                await bot.send_message(chat_id, f"💡 Может, ты имел в виду <b>{fixed}</b>? Ищу…", parse_mode="HTML")
                try:
                    new_hits = await kp.search(fixed, limit=5)
                    if new_hits:
                        hits = new_hits
                except Exception:
                    pass

        if not hits:
            await bot.send_message(chat_id, "🤷 Ничего не нашёл. Попробуй другое название (можно с годом или на оригинальном языке).")
            return

        if len(hits) == 1 and not force_picker:
            await _add_by_kp_id(bot, chat_id, tg_user_id, hits[0].kp_id, session_factory, kp)
            return

        # Готовим нумерованный список + помечаем те что уже добавлены
        digits = ["1\u20e3", "2\u20e3", "3\u20e3", "4\u20e3", "5\u20e3", "6\u20e3", "7\u20e3", "8\u20e3", "9\u20e3", "\U0001f51f"]
        already_added: set[int] = set()
        async with session_factory() as session:
            for h in hits[:10]:
                existing = await repo.get_series_by_kp_id(session, h.kp_id)
                if existing:
                    us = await repo.get_user_series(session, tg_user_id, existing.id)
                    if us:
                        already_added.add(h.kp_id)

        lines = ["\U0001f914 <b>\u0412\u044b \u0438\u043c\u0435\u0435\u0442\u0435 \u0432 \u0432\u0438\u0434\u0443:</b>", ""]
        for i, h in enumerate(hits[:10]):
            prefix = digits[i] if i < len(digits) else f"{i + 1}."
            mark = " \u2705" if h.kp_id in already_added else ""
            title_line = f"{prefix} <b>{h.title_ru}</b>"
            if h.year:
                title_line += f" ({h.year})"
            title_line += mark
            lines.append(title_line)
            extra_bits = []
            if h.title_en and h.title_en != h.title_ru:
                extra_bits.append(f"<i>{h.title_en}</i>")
            if h.rating_kp:
                extra_bits.append(f"\u2b50 \u041a\u041f {h.rating_kp:.1f}")
            kp_url = f"https://www.kinopoisk.ru/series/{h.kp_id}/"
            extra_bits.append(f'<a href="{kp_url}">\u041a\u041f</a>')
            lines.append("   " + " \u00b7 ".join(extra_bits))
            if h.short_description:
                short = h.short_description if len(h.short_description) <= 140 else h.short_description[:140].rstrip() + "\u2026"
                lines.append(f"   <i>{short}</i>")
            lines.append("")
        caption = "\n".join(lines).rstrip()

        # Media group из постеров (одним сообщением), caption у первого
        media: list[InputMediaPhoto] = []
        for h in hits[:10]:
            if not h.poster_url:
                continue
            cap = caption if not media else None  # caption только у первого
            try:
                media.append(InputMediaPhoto(media=h.poster_url, caption=cap, parse_mode="HTML" if cap else None))
            except Exception:
                pass
        if len(media) >= 2:
            try:
                await bot.send_media_group(chat_id=chat_id, media=media[:10])
            except Exception as e:
                logger.warning("send_media_group failed: %s -- fallback to text", e)
                await bot.send_message(chat_id, caption, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await bot.send_message(chat_id, caption, parse_mode="HTML", disable_web_page_preview=True)

        # Клавиатура для выбора цифрой -- одним сообщением
        kb_items = [(h.kp_id, h.title_ru + (f" ({h.year})" if h.year else "")) for h in hits]
        await bot.send_message(
            chat_id,
            "\U0001f447 \u041a\u0430\u043a\u043e\u0439 \u0438\u0437 \u043d\u0438\u0445? (\u0438\u043b\u0438 \u043d\u0430\u043f\u0438\u0448\u0438 \u043d\u043e\u043c\u0435\u0440 \u0446\u0438\u0444\u0440\u043e\u0439)",
            reply_markup=search_results_keyboard(kb_items),
        )
        # FSM: запомним список и ждём цифру
        if state is not None:
            await state.update_data(pick_hits=[h.kp_id for h in hits])
            await state.set_state(PickFSM.choosing)

    @router.callback_query(F.data.startswith("pick:"))
    async def cb_pick(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        arg = call.data.split(":")[1]
        if arg == "none":
            await call.answer("Ок, поищем заново")
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await call.message.answer(
                "🔎 Уточни запрос — напиши название иначе (можно с годом или на оригинальном языке).\n"
                "Или пришли постер фото 📸"
            )
            return
        kp_id = int(arg)
        await call.answer("Загружаю…")
        await _add_by_kp_id(call.bot, call.message.chat.id, call.from_user.id, kp_id, session_factory, kp)
        try:
            await call.message.delete()
        except Exception:
            pass

    # ============== Статусы (st:) — с обязательной оценкой для watched ==============

    @router.message(PickFSM.choosing, Command("cancel"))
    async def pick_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(PickFSM.choosing, F.text.regexp(r"^\d+$"))
    async def pick_by_number(message: Message, state: FSMContext) -> None:
        n = int(message.text.strip())
        data = await state.get_data()
        hits: list[int] = data.get("pick_hits", [])
        if not hits or n < 1 or n > len(hits):
            await message.answer(f"Номер от 1 до {len(hits)}.")
            return
        await state.clear()
        await _add_by_kp_id(message.bot, message.chat.id, message.from_user.id, hits[n - 1], session_factory, kp)

    @router.callback_query(F.data.startswith("st:"))
    async def cb_status(call: CallbackQuery) -> None:
        _, status, series_id_s = call.data.split(":")
        series_id = int(series_id_s)
        async with session_factory() as session:
            await repo.get_or_create_user(
                session,
                tg_id=call.from_user.id,
                username=call.from_user.username,
                full_name=call.from_user.full_name,
            )
            # set_user_series_status зеркалит на всех членов пары — список общий.
            await repo.set_user_series_status(session, call.from_user.id, series_id, status)
            await session.commit()
        await call.answer(f"✅ {STATUS_LABELS.get(status, status)}")
        # После добавления в «хочу» — предложить похожие через Groq
        if status == "want" and groq:
            from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
            await call.message.answer(
                "👀 В очереди! Подобрать похожие?",
                reply_markup=IKM(inline_keyboard=[[
                    IKB(text="🎬 Похожие сериалы", callback_data=f"simto:{series_id}")
                ]]),
            )

    # ============== Прогресс серий (prog:) ==============
    def _increment_episode(progress: Optional[str]) -> str:
        """S1E5 → S1E6; '12' → '13'; None → 'S1E1'."""
        if not progress:
            return "S1E1"
        m = re.match(r"S(\d+)E(\d+)", progress, re.IGNORECASE)
        if m:
            return f"S{m.group(1)}E{int(m.group(2)) + 1}"
        m = re.match(r"(\d+)", progress.strip())
        if m:
            return str(int(m.group(1)) + 1)
        return "S1E1"

    def _increment_season(progress: Optional[str]) -> str:
        """S1E5 → S2E1; None → 'S2E1'."""
        if not progress:
            return "S2E1"
        m = re.match(r"S(\d+)E\d+", progress, re.IGNORECASE)
        if m:
            return f"S{int(m.group(1)) + 1}E1"
        return "S2E1"

    def _progress_keyboard(series_id: int, current: Optional[str]):
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        rows = [
            [
                IKB(text="📺 +1 серия", callback_data=f"prog:ep:{series_id}"),
                IKB(text="🎬 +1 сезон", callback_data=f"prog:sn:{series_id}"),
            ],
            [
                IKB(text="✏️ Ввести вручную", callback_data=f"prog:set:{series_id}"),
            ],
        ]
        if current:
            rows.append([IKB(text="🔄 Сбросить прогресс", callback_data=f"prog:reset:{series_id}")])
        return IKM(inline_keyboard=rows)

    class ProgressFSM(StatesGroup):
        waiting = State()

    @router.callback_query(F.data.startswith("prog:"))
    async def cb_progress(call: CallbackQuery, state: FSMContext) -> None:
        parts = call.data.split(":")
        action = parts[1]
        if action not in ("ep", "sn", "set", "reset", "show"):
            await call.answer()
            return
        series_id = int(parts[2])

        if action == "set":
            await state.update_data(series_id=series_id)
            await state.set_state(ProgressFSM.waiting)
            await call.answer()
            await call.message.answer(
                "✏️ Напиши прогресс одним сообщением.\n"
                "Например: <code>S2E5</code> или <code>12</code> серий. /cancel чтобы отменить.",
                parse_mode="HTML",
            )
            return

        async with session_factory() as session:
            us = await repo.get_user_series(session, call.from_user.id, series_id)
            series = await session.get(Series, series_id)
            current = us.current_episode if us else None
            if action == "ep":
                new_value = _increment_episode(current)
            elif action == "sn":
                new_value = _increment_season(current)
            elif action == "reset":
                new_value = None
            else:
                new_value = current
            await repo.set_user_series_progress(session, call.from_user.id, series_id, new_value)
            await session.commit()

        title = series.title_ru if series else "сериал"
        if new_value:
            await call.answer(f"📺 {new_value}")
            await call.message.answer(
                f"📺 <b>{title}</b>: <code>{new_value}</code>",
                parse_mode="HTML",
                reply_markup=_progress_keyboard(series_id, new_value),
            )
        else:
            await call.answer("🔄 Прогресс сброшен")
            await call.message.answer(
                f"🔄 <b>{title}</b>: прогресс сброшен.",
                parse_mode="HTML",
                reply_markup=_progress_keyboard(series_id, None),
            )

    @router.message(ProgressFSM.waiting, Command("cancel"))
    async def progress_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(ProgressFSM.waiting)
    async def progress_set(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        series_id = data.get("series_id")
        text = (message.text or "").strip()
        if not series_id or not text or len(text) > 32:
            await message.answer("Странный ввод. Попробуй <code>S2E5</code> или /cancel", parse_mode="HTML")
            return
        async with session_factory() as session:
            await repo.set_user_series_progress(session, message.from_user.id, series_id, text)
            await session.commit()
        await state.clear()
        await message.answer(
            f"📺 Прогресс сохранён: <code>{text}</code>",
            parse_mode="HTML",
            reply_markup=_progress_keyboard(series_id, text),
        )

    # ============== Сезоны и серии (seasons:) ==============
    def _parse_dmy(date_str: Optional[str]) -> Optional[tuple[int, int, int]]:
        """«17.01.2025» → (2025, 1, 17). Иначе None."""
        if not date_str:
            return None
        try:
            d, m, y = date_str.split(".")
            return int(y), int(m), int(d)
        except Exception:
            return None

    @router.callback_query(F.data.startswith("seasons:"))
    async def cb_seasons(call: CallbackQuery) -> None:
        series_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            series = await session.get(Series, series_id)
            if series is None:
                await call.answer("Сериал не найден")
                return
            kp_id = series.kp_id
            title = series.title_ru
        await call.answer("Загружаю расписание…")
        loader = await start_loading(call.bot, call.message.chat.id, context="seasons")
        try:
            details = await kp.get_details(kp_id)
            if not details.is_series:
                async with session_factory() as session:
                    db_s = await session.get(Series, series_id)
                    if db_s:
                        db_s.is_series = False
                        await session.commit()
                await stop_loading(call.bot, call.message.chat.id, loader)
                await call.message.answer(
                    f"🎬 <b>{title}</b> — это фильм, а не сериал. "
                    f"Перерисую карточку с правильными кнопками.",
                    parse_mode="HTML",
                )
                return
            seasons = await kp.get_seasons(kp_id)
        except Exception as e:
            logger.exception("kp.get_seasons failed")
            await stop_loading(call.bot, call.message.chat.id, loader)
            await call.message.answer(f"😕 Не получилось загрузить сезоны: {e}")
            return
        await stop_loading(call.bot, call.message.chat.id, loader)
        if not seasons:
            await call.message.answer(
                f"📺 У <b>{title}</b> у KP пока нет данных по сезонам.\n"
                f"Возможно сериал ещё в производстве.",
                parse_mode="HTML",
            )
            return

        import datetime as _dt
        today = _dt.date.today()
        total_eps = sum(s.episodes_count or 0 for s in seasons)
        future_count = 0
        lines = [
            f"🎞 <b>{title}</b> · {len(seasons)} сез. · {total_eps} серий",
            "",
        ]
        # Полностью отображаем все сезоны, в каждом — до 8 серий (свежие)
        for season in seasons:
            head_bits = [f"<b>Сезон {season.number}</b>"]
            if season.air_date:
                head_bits.append(season.air_date.split(".")[-1])  # год
            if season.episodes_count:
                head_bits.append(f"{season.episodes_count} серий")
            lines.append("🎬 " + " · ".join(head_bits))

            eps = season.episodes
            # Показываем сначала вышедшие, затем ещё не вышедшие. Лимит 8 чтобы caption не разорвался
            shown = eps[-8:] if len(eps) > 8 else eps
            if len(eps) > 8:
                lines.append(f"  <i>… первые {len(eps) - 8} серий скрыто</i>")
            for ep in shown:
                tup = _parse_dmy(ep.air_date)
                is_future = False
                if tup:
                    is_future = _dt.date(*tup) > today
                    if is_future:
                        future_count += 1
                marker = "⏳" if is_future else "✅"
                date_part = f" — {ep.air_date}" if ep.air_date else ""
                name_part = f" «{ep.name}»" if ep.name else ""
                lines.append(f"  {marker} E{ep.number}{name_part}{date_part}")
            lines.append("")

        if future_count > 0:
            lines.append(f"⏳ <b>{future_count}</b> серий впереди — поставлю в подписку, нажми «🔔 Уведомлять» на карточке.")
        # Telegram лимит сообщения 4096 — обрезаем если слишком длинно
        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n…(обрезано)"
        await call.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    @router.callback_query(F.data.startswith("parts:"))
    async def cb_parts(call: CallbackQuery) -> None:
        """Связанные части серии (для фильмов): sequelsAndPrequels от KP."""
        series_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            series = await session.get(Series, series_id)
            if series is None:
                await call.answer("Не нашёл в БД")
                return
            kp_id = series.kp_id
            title = series.title_ru
        await call.answer("Ищу части…")
        await call.bot.send_chat_action(call.message.chat.id, action="typing")
        try:
            details = await kp.get_details(kp_id)
        except Exception as e:
            await call.message.answer(f"😕 Не получилось: {e}")
            return
        # Lazy-fix is_series — если KP считает что это сериал, перерисуем
        if details.is_series:
            async with session_factory() as session:
                db_s = await session.get(Series, series_id)
                if db_s:
                    db_s.is_series = True
                    await session.commit()
            await call.message.answer(
                f"📺 <b>{title}</b> — оказался сериалом! Перерисую с правильными кнопками.",
                parse_mode="HTML",
            )
            return

        related = details.related
        if not related:
            await call.message.answer(
                f"🎬 У <b>{title}</b> нет связанных частей у КП.\n"
                f"Это самостоятельный фильм или франшиза ещё не зафиксирована.",
                parse_mode="HTML",
            )
            return

        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        lines = [f"🎬 <b>{title}</b> · части серии ({len(related)}):", ""]
        rows: list[list] = []
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            my_rows = await repo.list_user_series(session, user.id, status=None)
            known_kp_ids = {s.kp_id for _, s in my_rows}
        for i, r in enumerate(related, 1):
            year_str = f" ({r.year})" if r.year else ""
            rating_str = f" ⭐{r.rating_kp:.1f}" if r.rating_kp else ""
            mark = " ✅" if r.kp_id in known_kp_ids else ""
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            lines.append(f"{marker} <b>{r.title_ru}</b>{year_str}{rating_str}{mark}")
            if r.kp_id not in known_kp_ids:
                short = r.title_ru[:22] + "…" if len(r.title_ru) > 22 else r.title_ru
                rows.append([IKB(text=f"➕ {i}. {short}", callback_data=f"addkp:{r.kp_id}")])
        if not rows:
            lines.append("")
            lines.append("<i>Все части уже в твоих списках ✅</i>")
        await call.message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=rows) if rows else None,
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data.startswith("notify:"))
    async def cb_notify_toggle(call: CallbackQuery) -> None:
        series_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            new_state = await repo.toggle_notify_releases(session, call.from_user.id, series_id)
            series = await session.get(Series, series_id)
            await session.commit()
        title = series.title_ru if series else "сериал"
        if new_state:
            await call.answer("🔔 Включил уведомления")
            # Контекстный текст: что именно будет отслеживаться
            status = (series.status_kp or "").lower() if series else ""
            is_unreleased = status in (
                "post-production", "pre-production", "announced", "filming", "in-production",
            ) or not series.year or (series.year and series.year > 2025)
            if is_unreleased:
                lines = [
                    f"🔔 Подписался на <b>{title}</b>.",
                    "",
                    "Напишу когда:",
                    "🎥 выйдет трейлер (если ещё нет)",
                    "📅 объявят/сдвинут дату премьеры (мир и Россия)",
                    "🎬 фильм выйдет в прокат / сериал стартует",
                ]
            else:
                lines = [
                    f"🔔 Подписался на <b>{title}</b>.",
                    "",
                    "Напишу когда:",
                    "🎬 выйдет новый сезон",
                    "🎥 появится новый трейлер",
                ]
            await call.message.answer("\n".join(lines), parse_mode="HTML")
        else:
            await call.answer("🔕 Выключил уведомления")
            await call.message.answer(
                f"🔕 Больше не буду писать про <b>{title}</b>.",
                parse_mode="HTML",
            )

    @router.callback_query(F.data.startswith("rm:"))
    async def cb_remove(call: CallbackQuery) -> None:
        series_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            series = await session.get(Series, series_id)
            title = series.title_ru if series else "сериал"
            removed = await repo.remove_user_series(session, call.from_user.id, series_id)
            await session.commit()
        if removed:
            await call.answer("🗑 Убрано из твоих списков", show_alert=False)
            try:
                # Скрыть кнопки под старой карточкой чтобы не запутать
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await call.message.answer(f"🗑 <b>{title}</b> убран из твоих списков", parse_mode="HTML")
        else:
            await call.answer("Этого сериала и так не было в твоих списках")

    @router.callback_query(F.data.startswith("simto:"))
    async def cb_similar_to(call: CallbackQuery) -> None:
        if not groq:
            await call.answer("Подбор ИИ недоступен")
            return
        series_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            series = await session.get(Series, series_id)
            if series is None:
                await call.answer("Сериал не найден")
                return
            # Не предлагать то что уже знаком ни мне, ни партнёру
            user = await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            my_rows = await repo.list_user_series(session, user.id, status=None)
            partner_rows: list = []
            if user.pair_id:
                members = await repo.get_pair_members(session, user.pair_id)
                for m in members:
                    if m.id != user.id:
                        partner_rows.extend(await repo.list_user_series(session, m.id, status=None))
            already = list({s.title_ru for _, s in my_rows} | {s.title_ru for _, s in partner_rows})
        await call.answer("Ищу похожие…")
        await call.bot.send_chat_action(call.message.chat.id, action="typing")
        try:
            suggestions = await groq.similar_to(
                title=series.title_ru, year=series.year, already_in_queue=already,
            )
        except Exception as e:
            logger.exception("Groq similar_to failed")
            await call.message.answer(f"😕 ИИ заглох: {e}")
            return
        if not suggestions:
            await call.message.answer("🤔 Не получилось придумать похожие.")
            return
        await send_suggestions_gallery(
            call.bot, call.message.chat.id, suggestions, kp,
            header=f"🎬 <b>Похожие на «{series.title_ru}»:</b>",
        )

    # ============== Weekly check-in (ck:) ==============
    @router.callback_query(F.data.startswith("ck:"))
    async def cb_checkin(call: CallbackQuery) -> None:
        _, action, series_id_s = call.data.split(":")
        series_id = int(series_id_s)
        if action == "fin":
            async with session_factory() as session:
                await repo.get_or_create_user(
                    session,
                    tg_id=call.from_user.id,
                    username=call.from_user.username,
                    full_name=call.from_user.full_name,
                )
                await repo.set_user_series_status(session, call.from_user.id, series_id, "watched")
                await session.commit()
            await call.answer("✅ Досмотрел")
        elif action == "cont":
            async with session_factory() as session:
                await repo.mark_checkin_sent(session, call.from_user.id, series_id)
                await session.commit()
            await call.answer("▶️ Спрошу через неделю")
        elif action == "drop":
            async with session_factory() as session:
                await repo.get_or_create_user(
                    session,
                    tg_id=call.from_user.id,
                    username=call.from_user.username,
                    full_name=call.from_user.full_name,
                )
                await repo.set_user_series_status(session, call.from_user.id, series_id, "dropped")
                await session.commit()
            await call.answer("❌ Дропнул")

    # ============== Трейлер ==============
    def _extract_yt_id_from_url(url: str) -> Optional[str]:
        """Если URL — YouTube, вытащить 11-символьный id. Иначе None."""
        if not url:
            return None
        m = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", url)
        return m.group(1) if m else None

    # Встроенный плеер Telegram (через скачивание mp4) выключен по умолчанию:
    # публичные Piped/Invidious инстансы блокируют /streams для серверных IP.
    # Включи через env INLINE_VIDEO_ENABLED=true когда поднимешь свой инстанс
    # Piped или Cobalt с JWT-ключом.
    import os as _os
    _inline_video_enabled = _os.getenv("INLINE_VIDEO_ENABLED", "").lower() in ("1", "true", "yes")

    async def _send_trailer_message(chat_id: int, bot: Bot, title: str, youtube_url: str, *, only_english: bool) -> None:
        """Шлёт сообщение с YouTube URL и кнопкой «Открыть в YouTube»."""
        lines = [f"🎥 Трейлер · <b>{title}</b>"]
        if only_english:
            lines.append("🇬🇧 Только английский трейлер")
        lines.append(youtube_url)
        await bot.send_message(
            chat_id,
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=False,
            reply_markup=trailer_link_keyboard(youtube_url),
        )

    async def _try_send_inline_video(
        chat_id: int, bot: Bot, *, title: str, youtube_id: str, only_english: bool,
        series_id_for_cache: Optional[int] = None,
    ) -> bool:
        """Пытается скачать стрим через Piped/Invidious и отправить
        через send_video (встроенный плеер Telegram). True если получилось."""
        from aiogram.types import BufferedInputFile

        await bot.send_chat_action(chat_id, action="upload_video")
        video_bytes = await trailer_finder.fetch_video_bytes(youtube_id, max_mb=45)
        if not video_bytes:
            return False
        try:
            caption_lines = [f"🎥 Трейлер · {title}"]
            if only_english:
                caption_lines.append("🇬🇧 Только английский")
            youtube_url = f"https://www.youtube.com/watch?v={youtube_id}"
            msg = await bot.send_video(
                chat_id=chat_id,
                video=BufferedInputFile(video_bytes, filename=f"trailer_{youtube_id}.mp4"),
                caption="\n".join(caption_lines),
                supports_streaming=True,
                reply_markup=trailer_link_keyboard(youtube_url),
            )
            # Кешируем file_id чтобы повторный клик был мгновенным
            if series_id_for_cache and msg.video and msg.video.file_id:
                async with session_factory() as session:
                    s = await session.get(Series, series_id_for_cache)
                    if s:
                        s.trailer_file_id = msg.video.file_id
                        await session.commit()
            return True
        except Exception as e:
            logger.warning("send_video failed for yt=%s: %s", youtube_id, e)
            return False

    @router.callback_query(F.data.startswith("tr:"))
    async def cb_trailer(call: CallbackQuery) -> None:
        series_id = int(call.data.split(":")[1])
        await call.answer()

        async with session_factory() as session:
            series = await session.get(Series, series_id)
            if series is None:
                await call.message.answer("Не нашёл сериал в БД.")
                return
            # Если когда-то закэшировали скачанный файл — попробуем его (мгновенно)
            if series.trailer_file_id:
                try:
                    await call.bot.send_video(
                        chat_id=call.message.chat.id,
                        video=series.trailer_file_id,
                        caption=f"🎥 Трейлер · {series.title_ru}",
                    )
                    return
                except Exception as e:
                    logger.warning("Cached file_id failed: %s", e)
                    series.trailer_file_id = None
                    await session.commit()
            yt_id = series.trailer_youtube_id
            url_cached = series.trailer_url
            title = series.title_ru
            title_en = series.title_en
            year = series.year
            trailer_lang = series.trailer_language
            kp_id = series.kp_id

        only_en = bool(trailer_lang and trailer_lang != "ru")

        # 1a) Кешированный yt_id — YouTube
        if yt_id:
            if _inline_video_enabled and await _try_send_inline_video(
                call.message.chat.id, call.bot,
                title=title, youtube_id=yt_id, only_english=only_en,
                series_id_for_cache=series_id,
            ):
                return
            url = f"https://www.youtube.com/watch?v={yt_id}"
            await _send_trailer_message(
                call.message.chat.id, call.bot, title, url, only_english=only_en,
            )
            return

        # 1b) Кешированный URL — не-YouTube (RuTube etc)
        if url_cached:
            await _send_trailer_message(
                call.message.chat.id, call.bot, title, url_cached, only_english=False,
            )
            return

        # 2) Ничего нет в БД — поищем через все источники
        loader = await start_loading(call.bot, call.message.chat.id, context="trailer")
        await call.bot.send_chat_action(call.message.chat.id, action="typing")
        imdb_id = tmdb_id = None
        is_series_flag = True
        try:
            details = await kp.get_details(kp_id)
            imdb_id = details.imdb_id
            tmdb_id = details.tmdb_id
            is_series_flag = details.is_series
            if details.best_trailer_youtube_id:
                yt_id = details.best_trailer_youtube_id
                trailer_lang = details.best_trailer_language
        except Exception as e:
            logger.warning("kp.get_details for trailer search failed: %s", e)

        found_url: Optional[str] = None
        if yt_id:
            found_url = f"https://www.youtube.com/watch?v={yt_id}"
        else:
            try:
                found_url = await trailer_finder.find(
                    title=title, year=year, title_en=title_en,
                    imdb_id=imdb_id, tmdb_id=tmdb_id,
                    is_series=is_series_flag,
                )
            except Exception as e:
                logger.exception("trailer_finder failed: %s", e)

        if found_url:
            extracted_yt = _extract_yt_id_from_url(found_url)
            async with session_factory() as session:
                s = await session.get(Series, series_id)
                if s:
                    if extracted_yt:
                        s.trailer_youtube_id = extracted_yt
                        if trailer_lang:
                            s.trailer_language = trailer_lang
                    else:
                        s.trailer_url = found_url
                        s.trailer_language = "ru"  # RuTube почти всегда русский
                    await session.commit()
            only_en_now = only_en if extracted_yt else False
            await stop_loading(call.bot, call.message.chat.id, loader)
            if extracted_yt and _inline_video_enabled and await _try_send_inline_video(
                call.message.chat.id, call.bot,
                title=title, youtube_id=extracted_yt, only_english=only_en_now,
                series_id_for_cache=series_id,
            ):
                return
            await _send_trailer_message(
                call.message.chat.id, call.bot, title, found_url, only_english=only_en_now,
            )
            return

        # 3) TG-канал
        tg_link = await find_trailer_tg_link(title, year)
        await stop_loading(call.bot, call.message.chat.id, loader)
        if tg_link:
            await call.message.answer(
                f"🎥 Нашёл трейлер в TG-канале: {tg_link}",
                disable_web_page_preview=False,
            )
            return

        # 4) Финальный fallback — страница поиска YouTube (всегда что-то покажет)
        search_url = build_youtube_search_url(title, year)
        await call.message.answer(
            f"🎥 Точного трейлера не нашёл. Глянь в поиске YouTube:\n{search_url}",
            disable_web_page_preview=False,
            reply_markup=trailer_link_keyboard(search_url),
        )

    # ============== Кнопки из /suggest: addkp: / seenkp: / trkp: ==============
    @router.callback_query(F.data.startswith("addkp:"))
    async def cb_add_kp(call: CallbackQuery) -> None:
        kp_id = int(call.data.split(":")[1])
        await call.answer("Добавляю…")
        await _add_by_kp_id(
            call.bot, call.message.chat.id, call.from_user.id, kp_id, session_factory, kp,
        )

    @router.callback_query(F.data.startswith("seenkp:"))
    async def cb_seen_kp(call: CallbackQuery) -> None:
        """«Уже смотрел» из предложений ИИ: сохраняем сериал в БД со
        статусом watched чтобы Groq больше его не предлагал."""
        kp_id = int(call.data.split(":")[1])
        await call.answer("Запомнил")
        try:
            details = await kp.get_details(kp_id)
        except Exception as e:
            logger.exception("KP details failed for seenkp")
            await call.message.answer(f"😕 Не получилось загрузить: {e}")
            return
        async with session_factory() as session:
            await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            await repo.upsert_series_from_dict(session, _details_to_series_dict(details))
            await session.commit()
            series = await repo.get_series_by_kp_id(session, details.kp_id)
            await repo.set_user_series_status(session, call.from_user.id, series.id, "watched")
            await session.commit()
        await call.message.answer(
            f"✅ <b>{details.title_ru}</b> отмечен как «Уже смотрел» — "
            f"в /suggest больше не предложу.",
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("skipkp:"))
    async def cb_skip_kp(call: CallbackQuery) -> None:
        """«❌ Не интересно» из предложений ИИ: сохраняем со статусом
        dropped — больше не предлагать (но в /list не попадёт)."""
        kp_id = int(call.data.split(":")[1])
        await call.answer("Запомнил")
        try:
            details = await kp.get_details(kp_id)
        except Exception as e:
            logger.exception("KP details failed for skipkp")
            await call.message.answer(f"😕 Не получилось загрузить: {e}")
            return
        async with session_factory() as session:
            await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            await repo.upsert_series_from_dict(session, _details_to_series_dict(details))
            await session.commit()
            series = await repo.get_series_by_kp_id(session, details.kp_id)
            await repo.set_user_series_status(session, call.from_user.id, series.id, "dropped")
            await session.commit()
        await call.message.answer(
            f"❌ <b>{details.title_ru}</b> — не интересно. "
            f"Больше не предложу.",
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("trkp:"))
    async def cb_trailer_kp(call: CallbackQuery) -> None:
        kp_id = int(call.data.split(":")[1])
        await call.answer()
        await call.bot.send_chat_action(call.message.chat.id, action="typing")
        try:
            details = await kp.get_details(kp_id)
        except Exception as e:
            logger.exception("KP details failed for trkp")
            await call.message.answer(f"😕 Не получилось загрузить: {e}")
            return

        title = details.title_ru
        year = details.year
        trailer_lang = details.best_trailer_language
        yt_id = details.best_trailer_youtube_id

        found_url: Optional[str] = None
        if yt_id:
            found_url = f"https://www.youtube.com/watch?v={yt_id}"
        else:
            try:
                found_url = await trailer_finder.find(
                    title=title, year=year, title_en=details.title_en,
                    imdb_id=details.imdb_id, tmdb_id=details.tmdb_id,
                    is_series=details.is_series,
                )
            except Exception as e:
                logger.warning("trailer_finder failed in trkp: %s", e)

        if found_url:
            extracted_yt = _extract_yt_id_from_url(found_url)
            only_en = bool(trailer_lang and trailer_lang != "ru") if extracted_yt else False
            if extracted_yt and _inline_video_enabled and await _try_send_inline_video(
                call.message.chat.id, call.bot,
                title=title, youtube_id=extracted_yt, only_english=only_en,
            ):
                return
            await _send_trailer_message(
                call.message.chat.id, call.bot, title, found_url, only_english=only_en,
            )
            return

        # TG-канал
        tg_link = await find_trailer_tg_link(title, year)
        if tg_link:
            await call.message.answer(
                f"🎥 Нашёл трейлер в TG-канале: {tg_link}",
                disable_web_page_preview=False,
            )
            return

        # Финальный fallback — YouTube search page
        search_url = build_youtube_search_url(title, year)
        await call.message.answer(
            f"🎥 Точного трейлера не нашёл. Глянь в поиске YouTube:\n{search_url}",
            disable_web_page_preview=False,
            reply_markup=trailer_link_keyboard(search_url),
        )

    # ============== Заметки (note:) — FSM ==============
    @router.callback_query(F.data.startswith("note:"))
    async def cb_note(call: CallbackQuery, state: FSMContext) -> None:
        series_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            series = await session.get(Series, series_id)
            title = series.title_ru if series else "сериал"
        await state.update_data(series_id=series_id)
        await state.set_state(NoteFSM.waiting)
        await call.message.answer(
            f"📝 Напиши заметку про <b>{title}</b> (или /cancel чтобы отменить):",
            parse_mode="HTML",
        )
        await call.answer()

    @router.message(NoteFSM.waiting, Command("cancel"))
    async def note_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(NoteFSM.waiting)
    async def note_save(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        series_id = data.get("series_id")
        text = (message.text or "").strip()[:500]
        async with session_factory() as session:
            us = await repo.get_user_series(session, message.from_user.id, series_id)
            if us is None:
                us = await repo.set_user_series_status(session, message.from_user.id, series_id, "want")
            us.notes = text
            await session.commit()
        await state.clear()
        await message.answer(f"✅ Заметка сохранена: <i>{text}</i>", parse_mode="HTML")

    # ============== Поделиться (share:) ==============
    @router.callback_query(F.data.startswith("share:"))
    async def cb_share(call: CallbackQuery) -> None:
        series_id = int(call.data.split(":")[1])
        bot_user = await call.bot.me()
        link = f"https://t.me/{bot_user.username}?start=show_{series_id}"
        await call.message.answer(
            f"📤 Перешли эту ссылку партнёру или другу:\n{link}",
            disable_web_page_preview=True,
        )
        await call.answer()

    # ============== Списки с пагинацией ==============
    _PAGE_SIZE = 10
    _SORT_LABELS = {"date": "📅 Дата", "rating": "⭐ Рейтинг", "year": "🆕 Год"}

    _HEADERS = {
        "want":         ("👀 <b>Наш общий «хочу посмотреть»:</b>", "Очередь пустая. Добавь /add &lt;название&gt;"),
        "watching":     ("▶️ <b>Смотрим сейчас:</b>", "Сейчас ничего не смотрим."),
        "watched":      ("✅ <b>Досмотрено:</b>", "Ещё ничего не досмотрели до конца."),
        "want_rewatch": ("🔁 <b>Хотим пересмотреть:</b>", "Список пересмотра пуст. Жми 🔁 в карточке досмотренного."),
    }

    def _sort_rows(rows: list, sort_key: str) -> list:
        """Сортирует [(UserSeries, Series), ...]. По умолчанию — по дате (updated_at DESC)."""
        if sort_key == "rating":
            return sorted(rows, key=lambda x: (x[1].rating_kp or 0), reverse=True)
        if sort_key == "year":
            return sorted(rows, key=lambda x: (x[1].year or 0), reverse=True)
        # 'date' по умолчанию: свежие добавления сверху
        return sorted(rows, key=lambda x: (x[0].updated_at or x[0].id), reverse=True)

    async def _render_list(
        bot: Bot,
        chat_id: int,
        tg_user_id: int,
        status: str,
        *,
        page: int = 0,
        sort_key: str = "date",
    ) -> None:
        header, empty_msg = _HEADERS.get(status, ("Список", "Пусто."))
        async with session_factory() as session:
            user = await repo.get_or_create_user(session, tg_id=tg_user_id, username=None, full_name=None)
            rows = await repo.list_user_series(session, user.id, status=status)
        if not rows:
            await bot.send_message(chat_id, empty_msg)
            return

        rows = _sort_rows(rows, sort_key)
        total = len(rows)
        total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        start = page * _PAGE_SIZE
        items = rows[start:start + _PAGE_SIZE]

        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM

        # Галерея постеров для текущей страницы
        media = [InputMediaPhoto(media=s.poster_url) for _, s in items if s.poster_url]
        if len(media) >= 2:
            try:
                await bot.send_media_group(chat_id, media[:10])
            except Exception as e:
                logger.warning("send_media_group failed for /%s page=%s: %s", status, page, e)

        # Текст: заголовок + пронумерованный список
        page_label = f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
        lines = [f"{header}  ·  {total} шт.{page_label}", ""]
        unreleased_count = sum(1 for _, s in rows if is_unreleased(s))
        if unreleased_count:
            lines[0] += f" · ⏳ {unreleased_count} ждём"
        for i, (us, s) in enumerate(items, 1):
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            year_str = f" ({s.year})" if s.year else ""
            rating_str = f" ⭐{s.rating_kp:.1f}" if s.rating_kp else ""
            unreleased_str = ""
            if is_unreleased(s):
                date = s.premiere_russia or s.premiere_world
                unreleased_str = f" · ⏳ <i>{date or 'не вышел'}</i>"
            lines.append(f"{marker} <b>{s.title_ru}</b>{year_str}{rating_str}{unreleased_str}")
        lines.append("")
        lines.append(f"<i>Сортировка: {_SORT_LABELS.get(sort_key, sort_key)} · 👇 жми номер чтобы открыть</i>")

        # 1) Ряд: кнопки сортировки (текущая отмечена ✓)
        sort_row = []
        for key, label in _SORT_LABELS.items():
            text = ("• " + label) if key == sort_key else label
            sort_row.append(IKB(text=text, callback_data=f"ls:{status}:{key}"))

        # 2) Ряды: глаза 👁 1..N (по 5 в ряд)
        eye_rows: list[list] = []
        cur: list = []
        for i, (_, s) in enumerate(items, 1):
            cur.append(IKB(text=f"👁 {i}", callback_data=f"open:{s.id}"))
            if len(cur) >= 5:
                eye_rows.append(cur)
                cur = []
        if cur:
            eye_rows.append(cur)

        # 3) Пагинация (если >1 страницы)
        pagination_row = []
        if total_pages > 1:
            if page > 0:
                pagination_row.append(IKB(text="⬅️ Назад", callback_data=f"lp:{status}:{sort_key}:{page - 1}"))
            pagination_row.append(IKB(text=f"{page + 1}/{total_pages}", callback_data="lp:noop"))
            if page < total_pages - 1:
                pagination_row.append(IKB(text="Вперёд ➡️", callback_data=f"lp:{status}:{sort_key}:{page + 1}"))

        # 4) Доп. экшены (только на первой странице чтоб не перегружать)
        action_row = []
        if page == 0:
            if status == "want":
                action_row.append(IKB(text="🎲 Случайный", callback_data="open_random:want"))
                if total >= 2:
                    action_row.append(IKB(text=f"▶️ Все в смотрю ({total})", callback_data="bulk:want:watching"))
            elif status == "watching" and total >= 2:
                action_row.append(IKB(text=f"✅ Все досмотрел ({total})", callback_data="bulk:watching:watched"))

        btn_rows = [sort_row, *eye_rows]
        if pagination_row:
            btn_rows.append(pagination_row)
        if action_row:
            btn_rows.append(action_row)

        await bot.send_message(
            chat_id,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=btn_rows),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data.startswith("lp:"))
    async def cb_list_page(call: CallbackQuery) -> None:
        parts = call.data.split(":")
        if len(parts) < 4 or parts[1] == "noop":
            await call.answer()
            return
        _, status, sort_key, page_s = parts
        await call.answer()
        await _render_list(
            call.bot, call.message.chat.id, call.from_user.id, status,
            page=int(page_s), sort_key=sort_key,
        )

    @router.callback_query(F.data.startswith("ls:"))
    async def cb_list_sort(call: CallbackQuery) -> None:
        _, status, sort_key = call.data.split(":")
        await call.answer(f"Сортировка: {_SORT_LABELS.get(sort_key, sort_key)}")
        await _render_list(
            call.bot, call.message.chat.id, call.from_user.id, status,
            page=0, sort_key=sort_key,
        )

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        await _render_list(message.bot, message.chat.id, message.from_user.id, "want")

    @router.message(Command("watching"))
    async def cmd_watching(message: Message) -> None:
        await _render_list(message.bot, message.chat.id, message.from_user.id, "watching")

    @router.message(Command("watched"))
    async def cmd_watched(message: Message) -> None:
        await _render_list(message.bot, message.chat.id, message.from_user.id, "watched")

    @router.message(Command("rewatch"))
    async def cmd_rewatch(message: Message) -> None:
        await _render_list(message.bot, message.chat.id, message.from_user.id, "want_rewatch")

    # Кнопки главного меню
    @router.message(F.text == "👀 Хочу")
    async def btn_want(message: Message) -> None:
        await cmd_list(message)

    @router.message(F.text == "▶️ Смотрю")
    async def btn_watching(message: Message) -> None:
        await cmd_watching(message)

    @router.message(F.text == "✅ Посмотрел")
    async def btn_watched(message: Message) -> None:
        await cmd_watched(message)

    @router.message(F.text == "🔁 Пересмотреть")
    async def btn_rewatch(message: Message) -> None:
        await cmd_rewatch(message)

    # ============== /random + /today ==============
    @router.message(Command("random"))
    async def cmd_random(message: Message) -> None:
        async with session_factory() as session:
            rows = await repo.list_user_series(session, message.from_user.id, status="want")
        # Не предлагаем то что ещё не вышло — посмотреть нельзя
        rows = [(us, s) for us, s in rows if not is_unreleased(s)]
        if not rows:
            await message.answer("Очередь пустая (или в ней только не вышедшие).")
            return
        us, series = random.choice(rows)
        await message.answer("🎲 А давай вот это:", parse_mode="HTML")
        await _send_card(message.bot, message.chat.id, series, user_status=us.status, user_rating=us.rating, note=us.notes)

    @router.message(Command("today"))
    async def cmd_today(message: Message) -> None:
        """Сводка по своим спискам + случайный сериал из watching/want/rewatch."""
        async with session_factory() as session:
            all_rows = await repo.list_user_series(session, message.from_user.id, status=None)
        if not all_rows:
            await message.answer("📭 Список пуст. Добавь хоть один сериал через /add 🎬")
            return

        # Сводка одной строкой
        st_count = Counter(us.status for us, _ in all_rows)
        summary_bits = []
        for status, emoji in [("watching", "▶️"), ("want", "👀"), ("want_rewatch", "🔁"), ("watched", "✅")]:
            if st_count.get(status):
                summary_bits.append(f"{emoji} {st_count[status]}")
        if summary_bits:
            await message.answer("📊 " + "  ·  ".join(summary_bits))

        # Случайный из активных: watching → want → rewatch. Не предлагаем ещё не вышедшие.
        for status, label in [("watching", "Ты ведь это смотришь"), ("want", "Из очереди"), ("want_rewatch", "Из пересмотра")]:
            rows = [(us, s) for us, s in all_rows if us.status == status and not is_unreleased(s)]
            if rows:
                us, series = random.choice(rows)
                await message.answer(f"🍿 <b>{label}:</b>", parse_mode="HTML")
                await _send_card(message.bot, message.chat.id, series, user_status=us.status, user_rating=us.rating, note=us.notes)
                return
        # Бывает что есть только watched/dropped — тогда ничего не показываем дополнительно
        await message.answer("Активных нет. Может что-то пересмотреть? 🔁")

    # ============== /wallpaper — постер «наша неделя» ==============
    @router.message(Command("wallpaper"))
    async def cmd_wallpaper(message: Message) -> None:
        loader = await start_loading(message.bot, message.chat.id, context="wallpaper")
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            rows = await repo.list_user_series(session, user.id, status=None)
        # Топ-15 активных: priority watching > want > rewatch > watched. Берём
        # с запасом — на постере покажем только те у кого есть poster_url.
        priority = {"watching": 0, "want": 1, "want_rewatch": 2, "watched": 3}
        active = sorted(
            ((us, s) for us, s in rows if us.status in priority and s.poster_url),
            key=lambda x: (priority.get(x[0].status, 9), -(x[0].updated_at.timestamp() if x[0].updated_at else 0)),
        )[:5]  # 5 с постерами
        status_labels = {"watching": "▶️ Смотрим", "want": "👀 Хотим", "want_rewatch": "🔁 Пересмотр", "watched": "✅ Досмотрено"}
        items = [(status_labels.get(us.status, "🎬"), s) for us, s in active]

        bot_user = await message.bot.me()
        png = await build_week_wallpaper(items, bot_username=bot_user.username)
        await stop_loading(message.bot, message.chat.id, loader)
        if not png:
            await message.answer("😕 Не получилось собрать постер. Попробуй позже.")
            return
        from aiogram.types import BufferedInputFile
        await message.bot.send_photo(
            message.chat.id,
            photo=BufferedInputFile(png, filename="our_week.png"),
            caption="📸 <b>Наша неделя</b> — можешь поделиться в сторис или с друзьями.",
            parse_mode="HTML",
        )

    # ============== /where — где смотреть конкретный сериал ==============
    async def _show_where_for_series(bot: Bot, chat_id: int, series: Series) -> None:
        """Показать платформы для конкретного сериала + кнопку открыть на КП."""
        opts: list = []
        if series.watch_options_json:
            try:
                opts = json.loads(series.watch_options_json)
            except Exception:
                opts = []
        lines = [f"📺 <b>Где смотреть «{series.title_ru}»:</b>", ""]
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        rows: list[list] = []
        if opts:
            for name, url in opts[:8]:
                lines.append(f"• <b>{name}</b>")
                rows.append([IKB(text=f"🎫 Открыть на {name}", url=url)])
        else:
            lines.append("<i>🤷 У Кинопоиска нет данных о платформах для этого тайтла.</i>")
        # Всегда даём ссылку на страницу самого КП — там могут быть пиратские/региональные опции
        if series.kp_id:
            kp_url = f"https://www.kinopoisk.ru/film/{series.kp_id}/"
            rows.append([IKB(text="🔗 Открыть на Кинопоиске", url=kp_url)])
        await bot.send_message(
            chat_id, "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=rows) if rows else None,
            disable_web_page_preview=True,
        )

    @router.message(Command("where"))
    async def cmd_where(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) >= 2:
            # «/where Severance» — ищем в твоих списках по подстроке
            query = parts[1].strip().lower()
            async with session_factory() as session:
                user = await repo.get_or_create_user(
                    session, tg_id=message.from_user.id,
                    username=message.from_user.username, full_name=message.from_user.full_name,
                )
                rows = await repo.list_user_series(session, user.id, status=None)
            matched = [
                (us, s) for us, s in rows
                if query in (s.title_ru or "").lower() or query in (s.title_en or "").lower()
            ]
            if not matched:
                await message.answer(
                    f"🤷 Не нашёл «{query}» в твоих списках. Добавь через /add.",
                )
                return
            await _show_where_for_series(message.bot, message.chat.id, matched[0][1])
            return

        # Без аргумента — список активных с кнопками выбора
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            rows = await repo.list_user_series(session, user.id, status=None)
        active = [(us, s) for us, s in rows if us.status in ("want", "watching", "want_rewatch", "watched")]
        if not active:
            await message.answer("📺 В списках ничего нет. Добавь через /add 🎬")
            return

        # Сортируем: watching → want → rewatch → watched
        order = {"watching": 0, "want": 1, "want_rewatch": 2, "watched": 3}
        active.sort(key=lambda x: (order.get(x[0].status, 9), -(x[1].rating_kp or 0)))
        items = active[:20]

        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        lines = ["📺 <b>Где смотреть — выбери:</b>", ""]
        btn_rows: list[list] = []
        for i, (us, s) in enumerate(items, 1):
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            st_emoji = {"want": "👀", "watching": "▶️", "watched": "✅", "want_rewatch": "🔁"}.get(us.status, "•")
            year_str = f" ({s.year})" if s.year else ""
            lines.append(f"{marker} {st_emoji} <b>{s.title_ru}</b>{year_str}")
            btn_rows.append([IKB(text=f"📺 {i}. {s.title_ru[:25]}", callback_data=f"where:{s.id}")])
        if len(active) > 20:
            lines.append("")
            lines.append(f"<i>… показал 20 из {len(active)}. Уточни через /where &lt;название&gt;</i>")
        lines.append("")
        lines.append("<i>👇 Выбери что хочешь смотреть — покажу платформы</i>")

        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=btn_rows),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data.startswith("where:"))
    async def cb_where(call: CallbackQuery) -> None:
        series_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            series = await session.get(Series, series_id)
            if series is None:
                await call.answer("Не нашёл в БД")
                return
        await call.answer()
        await _show_where_for_series(call.bot, call.message.chat.id, series)

    # ============== /poll — опрос «что включим сегодня» ==============
    # In-memory хранилище опросов. Не persistent. TTL 30 минут — старые
    # опросы вычищаются при создании нового.
    _polls: dict[str, dict] = {}
    _POLL_TTL_SEC = 30 * 60

    def _cleanup_old_polls() -> None:
        import time as _time
        now = _time.time()
        expired = [pid for pid, p in _polls.items() if now - p.get("created_at", 0) > _POLL_TTL_SEC]
        for pid in expired:
            _polls.pop(pid, None)
        if expired:
            logger.info("Cleaned up %d expired polls", len(expired))

    @router.message(Command("poll"))
    async def cmd_poll(message: Message) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            if not user.pair_id:
                await message.answer(
                    "📊 Опросы для пары. Сначала свяжись с партнёром: /pair",
                )
                return
            members = await repo.get_pair_members(session, user.pair_id)
            partner_ids = [m.id for m in members if m.id != user.id]
            if not partner_ids:
                await message.answer(
                    "📊 В паре никого кроме тебя. Подожди пока партнёр присоединится через /pair.",
                )
                return
            # Кандидаты: общий want + watching, исключая ещё не вышедшие
            rows = await repo.list_user_series(session, user.id, status=None)
            candidates = [
                s for us, s in rows
                if us.status in ("want", "watching") and not is_unreleased(s)
            ]

        if len(candidates) < 3:
            await message.answer(
                "📊 Нужно минимум 3 сериала в «👀 Хочу» / «▶️ Смотрю» чтобы провести опрос. "
                "Добавь больше через /add 🎬",
            )
            return

        _cleanup_old_polls()  # вычищаем протухшие опросы перед созданием нового

        chosen = random.sample(candidates, 3)
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        import secrets as _secrets
        import time as _time
        poll_id = _secrets.token_urlsafe(6)
        _polls[poll_id] = {
            "options": [(s.id, s.title_ru, s.year) for s in chosen],
            "votes": {},
            "participants": {user.id, *partner_ids},
            "initiator_name": message.from_user.full_name or message.from_user.username or "Партнёр",
            "created_at": _time.time(),
        }

        kb_rows = [
            [IKB(
                text=f"{i+1}. {title}" + (f" ({year})" if year else ""),
                callback_data=f"pollv:{poll_id}:{i}",
            )]
            for i, (sid, title, year) in enumerate(_polls[poll_id]["options"])
        ]
        body = (
            f"📊 <b>Что включим сегодня?</b>\n"
            f"Голосуйте оба — бот покажет совпадение."
        )
        # Шлём обоим
        await message.answer(body, parse_mode="HTML", reply_markup=IKM(inline_keyboard=kb_rows))
        for pid in partner_ids:
            try:
                await message.bot.send_message(
                    chat_id=pid,
                    text=f"📊 <b>{_polls[poll_id]['initiator_name']}</b> предлагает выбрать что включить:\n"
                         f"Голосуй ниже — бот покажет совпадение.",
                    parse_mode="HTML",
                    reply_markup=IKM(inline_keyboard=kb_rows),
                )
            except Exception as e:
                logger.warning("Poll invite to %s failed: %s", pid, e)
                await message.answer(f"⚠️ Не смог написать партнёру (он не открывал бота?). ID {pid}")

    @router.callback_query(F.data.startswith("pollv:"))
    async def cb_poll_vote(call: CallbackQuery) -> None:
        _, poll_id, opt_idx_s = call.data.split(":")
        opt_idx = int(opt_idx_s)
        _cleanup_old_polls()
        poll = _polls.get(poll_id)
        if not poll:
            await call.answer("Опрос закрыт (истёк или уже завершён). Создай новый: /poll", show_alert=True)
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        if call.from_user.id not in poll["participants"]:
            await call.answer("Этот опрос не для тебя.", show_alert=True)
            return

        poll["votes"][call.from_user.id] = opt_idx
        title = poll["options"][opt_idx][1]
        await call.answer(f"✅ Голос: {title}")

        # Все ли проголосовали?
        if len(poll["votes"]) >= len(poll["participants"]):
            votes_list = list(poll["votes"].values())
            if len(set(votes_list)) == 1:
                # Совпало!
                winner = poll["options"][votes_list[0]]
                title, year = winner[1], winner[2]
                year_str = f" ({year})" if year else ""
                msg = f"🎉 <b>Решили вместе:</b>\n🎬 <b>{title}</b>{year_str}\n\nХорошего вечера 🍿"
            else:
                opts_text = "\n".join(
                    f"  • {poll['options'][i][1]}" for i in votes_list
                )
                msg = (
                    f"🤔 <b>Не совпало</b>\n"
                    f"Голоса:\n{opts_text}\n\n"
                    f"Можно проголосовать ещё раз или /poll для новых вариантов."
                )
            # Шлём результат всем участникам
            for uid in poll["participants"]:
                try:
                    await call.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
                except Exception:
                    pass
            # Чистим
            _polls.pop(poll_id, None)

    # ============== /cinema — афиша в твоём городе ==============
    # Slug'и Кинопоиска для главных городов России (для URL .../afisha/new/city/<slug>/)
    _CINEMA_CITIES = [
        ("moscow",            "Москва"),
        ("saint-petersburg",  "Санкт-Петербург"),
        ("novosibirsk",       "Новосибирск"),
        ("ekaterinburg",      "Екатеринбург"),
        ("nizhny-novgorod",   "Нижний Новгород"),
        ("kazan",             "Казань"),
        ("chelyabinsk",       "Челябинск"),
        ("samara",            "Самара"),
        ("rostov-na-donu",    "Ростов-на-Дону"),
        ("ufa",                "Уфа"),
        ("krasnoyarsk",       "Красноярск"),
        ("voronezh",          "Воронеж"),
        ("perm",              "Пермь"),
        ("volgograd",         "Волгоград"),
        ("krasnodar",         "Краснодар"),
    ]
    _CITY_BY_SLUG = dict(_CINEMA_CITIES)

    class CityFSM(StatesGroup):
        waiting = State()

    def _city_picker_kb():
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        rows: list[list] = []
        cur: list = []
        for slug, name in _CINEMA_CITIES:
            cur.append(IKB(text=name, callback_data=f"city:{slug}"))
            if len(cur) >= 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        rows.append([IKB(text="🌍 Другой город — введу сам", callback_data="city:other")])
        return IKM(inline_keyboard=rows)

    async def _send_cinema_for_city(bot: Bot, chat_id: int, city_slug: str, city_name: str) -> None:
        loader = await start_loading(bot, chat_id, context="cinema")
        try:
            hits = await kp.get_movies_in_theaters(limit=15)
        except Exception as e:
            await stop_loading(bot, chat_id, loader)
            await bot.send_message(chat_id, f"😕 KP заглох: {e}")
            return
        await stop_loading(bot, chat_id, loader)
        if not hits:
            await bot.send_message(chat_id, "🎫 Сейчас в прокате ничего не нашлось у KP.")
            return

        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM

        # media_group из постеров
        media = [InputMediaPhoto(media=h.poster_url) for h in hits[:10] if h.poster_url]
        if len(media) >= 2:
            try:
                await bot.send_media_group(chat_id, media[:10])
            except Exception as e:
                logger.warning("send_media_group failed for /cinema: %s", e)

        afisha_url = f"https://www.kinopoisk.ru/afisha/new/city/{city_slug}/"
        lines = [
            f"🎫 <b>В прокате · {city_name}</b>",
            "",
        ]
        for i, h in enumerate(hits[:10], 1):
            year_str = f" ({h.year})" if h.year else ""
            rating_str = f" ⭐{h.rating_kp:.1f}" if h.rating_kp else ""
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            lines.append(f"{marker} <b>{h.title_ru}</b>{year_str}{rating_str}")
        lines.append("")
        lines.append(f"<i>🎟 Полная афиша с сеансами — на Кинопоиске:</i>")
        lines.append(afisha_url)

        # Кнопки: открыть афишу города (URL-кнопка) + сеансы для каждого фильма + смена города
        rows: list[list] = [
            [IKB(text=f"🎫 Открыть афишу {city_name}", url=afisha_url)],
        ]
        # Для каждого фильма — отдельная кнопка-URL с прямым переходом
        for i, h in enumerate(hits[:6], 1):
            short = h.title_ru[:22] + "…" if len(h.title_ru) > 22 else h.title_ru
            sessions_url = f"https://www.kinopoisk.ru/film/{h.kp_id}/sessions/city/{city_slug}/"
            rows.append([IKB(text=f"🎟 Сеансы — {short}", url=sessions_url)])
        rows.append([IKB(text="📍 Сменить город", callback_data="cinema:change_city")])

        await bot.send_message(
            chat_id,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=rows),
            disable_web_page_preview=True,
        )

    @router.message(Command("cinema"))
    async def cmd_cinema(message: Message) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            saved_city = user.city
        if saved_city and saved_city in _CITY_BY_SLUG:
            await _send_cinema_for_city(
                message.bot, message.chat.id, saved_city, _CITY_BY_SLUG[saved_city],
            )
            return
        if saved_city:
            # Юзер ввёл произвольный город — используем его slug как есть
            await _send_cinema_for_city(
                message.bot, message.chat.id, saved_city, saved_city.replace("-", " ").title(),
            )
            return
        await message.answer(
            "🎫 <b>Какой у вас город?</b>\n"
            "(сохраню, чтобы потом не спрашивать)",
            parse_mode="HTML",
            reply_markup=_city_picker_kb(),
        )

    @router.callback_query(F.data == "cinema:change_city")
    async def cb_cinema_change_city(call: CallbackQuery) -> None:
        await call.answer()
        await call.message.answer(
            "📍 <b>Выбери город:</b>",
            parse_mode="HTML",
            reply_markup=_city_picker_kb(),
        )

    @router.callback_query(F.data.startswith("city:"))
    async def cb_city_picked(call: CallbackQuery, state: FSMContext) -> None:
        slug = call.data.split(":", 1)[1]
        if slug == "other":
            await state.set_state(CityFSM.waiting)
            await call.answer()
            await call.message.answer(
                "✏️ Введи название города по-русски или его slug на КП.\n"
                "Примеры: <code>сочи</code>, <code>vladivostok</code>. "
                "Если slug неверный — Кинопоиск покажет общую афишу.\n"
                "/cancel чтобы отменить.",
                parse_mode="HTML",
            )
            return
        # Знакомый slug из списка
        if slug not in _CITY_BY_SLUG:
            await call.answer("Неизвестный город", show_alert=True)
            return
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            user.city = slug
            await session.commit()
        await call.answer(f"📍 {_CITY_BY_SLUG[slug]}")
        await _send_cinema_for_city(
            call.bot, call.message.chat.id, slug, _CITY_BY_SLUG[slug],
        )

    @router.message(CityFSM.waiting, Command("cancel"))
    async def cb_city_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(CityFSM.waiting)
    async def cb_city_typed(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if not raw or len(raw) > 64:
            await message.answer("Странный город. Попробуй ещё раз или /cancel")
            return
        # Эвристика: если уже похоже на slug (латиница с дефисами) — используем как есть.
        # Иначе пробуем перевести в slug через простой transliterate.
        if re.fullmatch(r"[a-z0-9-]+", raw.lower()):
            slug = raw.lower()
            display = slug.replace("-", " ").title()
        else:
            # Простая транслитерация ru → kinopoisk slug
            translit = {
                "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh",
                "з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o",
                "п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts",
                "ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"","э":"e",
                "ю":"yu","я":"ya"," ":"-",
            }
            slug = "".join(translit.get(ch, ch) for ch in raw.lower())
            slug = re.sub(r"[^a-z0-9-]", "", slug)
            display = raw.title()
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            user.city = slug
            await session.commit()
        await state.clear()
        await _send_cinema_for_city(message.bot, message.chat.id, slug, display)

    # ============== /profile — настройки + статистика ==============
    class MinRatingFSM(StatesGroup):
        waiting = State()

    async def _render_profile(message_or_call, edit: bool = False) -> None:
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        if isinstance(message_or_call, CallbackQuery):
            tg_id = message_or_call.from_user.id
            chat_id = message_or_call.message.chat.id
            from_user = message_or_call.from_user
            bot_obj = message_or_call.bot
        else:
            tg_id = message_or_call.from_user.id
            chat_id = message_or_call.chat.id
            from_user = message_or_call.from_user
            bot_obj = message_or_call.bot

        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=tg_id,
                username=from_user.username, full_name=from_user.full_name,
            )
            rows = await repo.list_user_series(session, user.id, status=None)
            blocked_genres = await repo.list_blacklisted_genres(session, user)
            partner_name = None
            if user.pair_id:
                members = await repo.get_pair_members(session, user.pair_id)
                for m in members:
                    if m.id != user.id:
                        partner_name = m.full_name or (f"@{m.username}" if m.username else f"id={m.id}")
                        break

        # Stats
        st_count = Counter(us.status for us, _ in rows)
        genre_counter: Counter = Counter()
        for us, s in rows:
            if us.status in ("want", "watching", "watched", "want_rewatch") and s.genres:
                for g in s.genres.split(","):
                    g = g.strip()
                    if g:
                        genre_counter[g] += 1
        top_genres = ", ".join(g for g, _ in genre_counter.most_common(5)) or "—"

        min_rating = user.min_rating if user.min_rating is not None else 7.0
        city_label = user.city or "не выбран"

        lines = [
            f"👤 <b>Профиль · {from_user.full_name or '@'+(from_user.username or '')}</b>",
            "",
            f"👫 Пара: <b>{partner_name}</b>" if partner_name else "👫 Пара: <i>не настроена</i> (см. /pair)",
            f"📍 Город для /cinema: <b>{city_label}</b>",
            f"⭐ Мин. рейтинг для /trending и подсказок: <b>{min_rating:.1f}</b>",
            f"🚫 Заблокировано жанров: <b>{len(blocked_genres)}</b>",
            "",
            "📊 <b>Статистика:</b>",
            f"📺 Всего: <b>{len(rows)}</b>",
            f"👀 Хочу: {st_count.get('want', 0)}",
            f"▶️ Смотрю: {st_count.get('watching', 0)}",
            f"✅ Досмотрел: {st_count.get('watched', 0)}",
            f"🔁 Пересмотр: {st_count.get('want_rewatch', 0)}",
            f"❌ Дропнул: {st_count.get('dropped', 0)}",
            f"🎭 Топ жанры: <i>{top_genres}</i>",
        ]

        rows_kb = [
            [IKB(text=f"⭐ Сменить мин. рейтинг ({min_rating:.1f})", callback_data="prof:rating")],
            [IKB(text="📍 Сменить город", callback_data="cinema:change_city")],
            [IKB(text="🚫 Жанры-табу", callback_data="prof:blacklist")],
            [IKB(text="🔥 Громкие новинки", callback_data="prof:trending")],
        ]
        markup = IKM(inline_keyboard=rows_kb)

        if edit and isinstance(message_or_call, CallbackQuery):
            try:
                await message_or_call.message.edit_text(
                    "\n".join(lines), parse_mode="HTML", reply_markup=markup,
                )
                return
            except Exception:
                pass
        await bot_obj.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=markup)

    @router.message(Command("profile"))
    async def cmd_profile(message: Message) -> None:
        await _render_profile(message)

    @router.callback_query(F.data == "prof:rating")
    async def cb_profile_rating(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(MinRatingFSM.waiting)
        await call.answer()
        await call.message.answer(
            "⭐ Введи минимальный рейтинг КП для подбора (от 0.0 до 10.0).\n"
            "Например <code>7.5</code>. /cancel — отмена",
            parse_mode="HTML",
        )

    @router.message(MinRatingFSM.waiting, Command("cancel"))
    async def min_rating_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(MinRatingFSM.waiting)
    async def min_rating_typed(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip().replace(",", ".")
        try:
            val = float(text)
        except Exception:
            await message.answer("Нужно число, например <code>7.5</code>", parse_mode="HTML")
            return
        if val < 0 or val > 10:
            await message.answer("Рейтинг 0.0–10.0, попробуй ещё.")
            return
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            user.min_rating = val
            await session.commit()
        await state.clear()
        await message.answer(f"⭐ Минимальный рейтинг: <b>{val:.1f}</b>", parse_mode="HTML")
        await _render_profile(message)

    @router.callback_query(F.data == "prof:blacklist")
    async def cb_profile_bl(call: CallbackQuery) -> None:
        await call.answer()
        await _render_blacklist(call)

    @router.callback_query(F.data == "prof:trending")
    async def cb_profile_trending(call: CallbackQuery) -> None:
        await call.answer()
        await cmd_trending(call.message)

    # ============== /trending — громкие новинки ==============
    @router.message(Command("trending"))
    async def cmd_trending(message: Message) -> None:
        loader = await start_loading(message.bot, message.chat.id, context="suggest")
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            min_rating = user.min_rating if user.min_rating is not None else 7.0
            my_rows = await repo.list_user_series(session, user.id, status=None)
            known = {s.kp_id for _, s in my_rows}
            blocked = {g.lower() for g in await repo.list_blacklisted_genres(session, user)}

        try:
            # Берём с запасом — после blacklist-фильтра может остаться меньше
            hits = await kp.get_trending(min_rating=min_rating, days_back=60, limit=30)
        except Exception as e:
            await stop_loading(message.bot, message.chat.id, loader)
            await message.answer(f"😕 KP заглох: {e}")
            return
        # Фильтруем по blacklist жанров (case-insensitive, по любому из жанров тайтла)
        def _allowed(h) -> bool:
            if h.kp_id in known:
                return False
            if blocked and any(g.lower() in blocked for g in (h.genres or [])):
                return False
            return True
        fresh = [h for h in hits if _allowed(h)][:10]
        await stop_loading(message.bot, message.chat.id, loader)
        if not fresh:
            await message.answer(
                f"🔥 Новинок с рейтингом ≥ {min_rating:.1f} не нашлось. "
                f"Можешь снизить порог в /profile.",
            )
            return

        media = [InputMediaPhoto(media=h.poster_url) for h in fresh[:10] if h.poster_url]
        if len(media) >= 2:
            try:
                await message.bot.send_media_group(message.chat.id, media[:10])
            except Exception:
                pass
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        lines = [f"🔥 <b>Громкие новинки</b> (рейтинг ≥ {min_rating:.1f}):", ""]
        rows_kb: list[list] = []
        cur: list = []
        for i, h in enumerate(fresh, 1):
            year_str = f" ({h.year})" if h.year else ""
            rating_str = f" ⭐{h.rating_kp:.1f}" if h.rating_kp else ""
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            lines.append(f"{marker} <b>{h.title_ru}</b>{year_str}{rating_str}")
            short = h.title_ru[:18] + "…" if len(h.title_ru) > 18 else h.title_ru
            cur.append(IKB(text=f"➕ {i}. {short}", callback_data=f"addkp:{h.kp_id}"))
            if len(cur) >= 2:
                rows_kb.append(cur)
                cur = []
        if cur:
            rows_kb.append(cur)
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=rows_kb),
            disable_web_page_preview=True,
        )

    # ============== /upcoming — премьеры под жанры пары ==============
    @router.message(Command("upcoming"))
    async def cmd_upcoming(message: Message) -> None:
        loader = await start_loading(message.bot, message.chat.id, context="suggest")
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            all_rows = await repo.list_user_series(session, user.id, status=None)
            if user.pair_id:
                members = await repo.get_pair_members(session, user.pair_id)
                for m in members:
                    if m.id != user.id:
                        all_rows.extend(await repo.list_user_series(session, m.id, status=None))
            known_kp_ids = {s.kp_id for _, s in all_rows}
            gc: Counter = Counter()
            for us, s in all_rows:
                if us.status != "dropped" and s.genres:
                    for g in s.genres.split(","):
                        g = g.strip()
                        if g:
                            gc[g] += 1
            top_genres = [g for g, _ in gc.most_common(3)]
            blocked = {g.lower() for g in await repo.list_blacklisted_genres(session, user)}

        try:
            if top_genres:
                hits = await kp.get_upcoming_series(genres=top_genres, limit=25)
            else:
                hits = await kp.get_upcoming_series(limit=25)
        except Exception as e:
            await stop_loading(message.bot, message.chat.id, loader)
            await message.answer(f"😕 KP заглох: {e}")
            return
        def _ok(h) -> bool:
            if h.kp_id in known_kp_ids:
                return False
            if blocked and any(g.lower() in blocked for g in (h.genres or [])):
                return False
            return True
        fresh = [h for h in hits if _ok(h)][:10]
        await stop_loading(message.bot, message.chat.id, loader)
        if not fresh:
            await message.answer(
                "📅 Не нашёл новых премьер под ваши жанры.\n"
                "Попробуй позже или /swipe для случайных предложений.",
            )
            return

        genres_hint = f" по жанрам: <i>{', '.join(top_genres)}</i>" if top_genres else ""
        lines = [f"📅 <b>Премьеры под вас{genres_hint}:</b>", ""]
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        for i, h in enumerate(fresh, 1):
            year_str = f" ({h.year})" if h.year else ""
            rating_str = f" ⭐{h.rating_kp:.1f}" if h.rating_kp else ""
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            lines.append(f"{marker} <b>{h.title_ru}</b>{year_str}{rating_str}")
        # media_group из постеров
        media = [InputMediaPhoto(media=h.poster_url) for h in fresh if h.poster_url]
        if len(media) >= 2:
            try:
                await message.bot.send_media_group(message.chat.id, media[:10])
            except Exception as e:
                logger.warning("send_media_group failed for /upcoming: %s", e)

        # Один ряд кнопок «➕ Добавить» под каждым (компактно — по 2 в ряд)
        rows: list[list] = []
        cur: list = []
        for i, h in enumerate(fresh, 1):
            short = h.title_ru[:18] + "…" if len(h.title_ru) > 18 else h.title_ru
            cur.append(IKB(text=f"➕ {i}. {short}", callback_data=f"addkp:{h.kp_id}"))
            if len(cur) >= 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)

        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=rows),
            disable_web_page_preview=True,
        )

    # ============== /top <жанр> — топ-10 по жанру ==============
    @router.message(Command("top"))
    async def cmd_top(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "📊 Напиши жанр: <code>/top драма</code>\n\n"
                "Доступные: драма, триллер, комедия, фантастика, ужасы, мелодрама, "
                "детектив, боевик, фэнтези, криминал, мультфильм, биография, история",
                parse_mode="HTML",
            )
            return
        genre = parts[1].strip().lower()
        loader = await start_loading(message.bot, message.chat.id, context="suggest")
        try:
            hits = await kp.get_top_by_genre(genre, is_series=True, limit=10)
            if not hits:
                hits = await kp.get_top_by_genre(genre, limit=10)
        except Exception as e:
            await stop_loading(message.bot, message.chat.id, loader)
            await message.answer(f"😕 KP заглох: {e}")
            return
        await stop_loading(message.bot, message.chat.id, loader)
        if not hits:
            await message.answer(
                f"🤷 По жанру «{genre}» ничего не нашёл. Проверь написание.",
            )
            return

        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            my_rows = await repo.list_user_series(session, user.id, status=None)
            known_kp_ids = {s.kp_id for _, s in my_rows}
            blocked = {g.lower() for g in await repo.list_blacklisted_genres(session, user)}
        # Если юзер просит /top драма, а у него «драма» в blacklist — это его конфликт,
        # не фильтруем сам запрошенный жанр. Только другие блок-жанры
        blocked_filter = blocked - {genre.lower()}
        if blocked_filter:
            hits = [h for h in hits if not any(g.lower() in blocked_filter for g in (h.genres or []))]
            if not hits:
                await message.answer(
                    f"🤷 По жанру «{genre}» нашлись тайтлы, но все попали под твой "
                    f"чёрный список ({', '.join(sorted(blocked_filter))}). "
                    f"Поправь в /blacklist."
                )
                return

        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        lines = [f"📊 <b>Топ-{len(hits)} по жанру «{genre}»:</b>", ""]
        for i, h in enumerate(hits, 1):
            year_str = f" ({h.year})" if h.year else ""
            rating_str = f" ⭐{h.rating_kp:.1f}" if h.rating_kp else ""
            mark = " ✅" if h.kp_id in known_kp_ids else ""
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            lines.append(f"{marker} <b>{h.title_ru}</b>{year_str}{rating_str}{mark}")

        media = [InputMediaPhoto(media=h.poster_url) for h in hits if h.poster_url]
        if len(media) >= 2:
            try:
                await message.bot.send_media_group(message.chat.id, media[:10])
            except Exception:
                pass

        rows: list[list] = []
        cur: list = []
        for i, h in enumerate(hits, 1):
            if h.kp_id in known_kp_ids:
                continue  # уже добавлено, не предлагаем
            short = h.title_ru[:18] + "…" if len(h.title_ru) > 18 else h.title_ru
            cur.append(IKB(text=f"➕ {i}. {short}", callback_data=f"addkp:{h.kp_id}"))
            if len(cur) >= 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        if not rows:
            lines.append("")
            lines.append("<i>Все уже в твоих списках ✅</i>")

        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=rows) if rows else None,
            disable_web_page_preview=True,
        )

    @router.message(F.text == "🎲 Что включить?")
    async def btn_today(message: Message) -> None:
        await cmd_today(message)

    # ============== /find — поиск в своих ==============
    @router.message(Command("find"))
    async def cmd_find(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("🔎 Напиши: <code>/find дарк</code>", parse_mode="HTML")
            return
        query = parts[1].strip().lower()
        async with session_factory() as session:
            rows = await repo.list_user_series(session, message.from_user.id, status=None)
        matched = [
            (us, series) for us, series in rows
            if query in (series.title_ru or "").lower() or query in (series.title_en or "").lower()
        ]
        if not matched:
            await message.answer(f"Ничего не нашёл по «{query}».")
            return
        await message.answer(f"🔎 Нашёл ({len(matched)}):", parse_mode="HTML")
        for us, series in matched[:10]:
            await _send_card(message.bot, message.chat.id, series, user_status=us.status, user_rating=us.rating, note=us.notes)

    # ============== /stats ==============
    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        async with session_factory() as session:
            all_rows = await repo.list_user_series(session, message.from_user.id, status=None)
        if not all_rows:
            await message.answer("📊 Пока пусто. Добавь что-нибудь через /add 🎬")
            return
        st_count = Counter(us.status for us, _ in all_rows)
        genre_counter: Counter = Counter()
        for us, s in all_rows:
            # «Нравящиеся» = всё что не dropped
            if us.status != "dropped" and s.genres:
                for g in s.genres.split(","):
                    g = g.strip()
                    if g:
                        genre_counter[g] += 1
        top_genres = ", ".join(g for g, _ in genre_counter.most_common(3)) or "—"
        text = (
            "📊 <b>Статистика</b>\n\n"
            f"📺 Всего сериалов/фильмов: <b>{len(all_rows)}</b>\n"
            f"👀 Хочу: {st_count.get('want', 0)}\n"
            f"▶️ Смотрю: {st_count.get('watching', 0)}\n"
            f"✅ Досмотрел: {st_count.get('watched', 0)}\n"
            f"🔁 Пересмотр: {st_count.get('want_rewatch', 0)}\n"
            f"❌ Дропнул: {st_count.get('dropped', 0)}\n\n"
            f"🎭 Топ жанры: <i>{top_genres}</i>"
        )
        await message.answer(text, parse_mode="HTML")

    @router.message(F.text == "📊 Статистика")
    async def btn_stats(message: Message) -> None:
        # Кнопка из старой раскладки — обновим клавиатуру на новую
        await cmd_stats(message)
        await message.answer("Кстати, меню снизу обновилось 👇", reply_markup=main_menu())

    @router.message(F.text == "👤 Профиль")
    async def btn_profile(message: Message) -> None:
        await cmd_profile(message)

    @router.message(F.text == "🔥 Новинки")
    async def btn_trending(message: Message) -> None:
        await cmd_trending(message)

    # ============== /suggest — Groq AI рекомендации с выбором ==============
    _SUGGEST_TYPES = [
        ("any",    "🎲 Любой"),
        ("series", "📺 Сериал"),
        ("movie",  "🎬 Фильм"),
    ]
    _SUGGEST_GENRES = [
        ("any",         "🎲 Любой жанр"),
        ("драма",       "🎭 Драма"),
        ("триллер",     "🔪 Триллер"),
        ("комедия",     "😄 Комедия"),
        ("фантастика",  "🚀 Фантастика"),
        ("ужасы",       "👻 Ужасы"),
        ("мелодрама",   "❤️ Мелодрама"),
        ("детектив",    "🔍 Детектив"),
        ("боевик",      "💥 Боевик"),
        ("фэнтези",     "🧙 Фэнтези"),
        ("приключения", "🗺 Приключения"),
        ("криминал",    "🕴 Криминал"),
        ("мультфильм",  "🎨 Мультфильм"),
        ("аниме",       "🇯🇵 Аниме"),
        ("документальный", "📚 Документальный"),
    ]
    # ASCII slugs! Cyrillic в callback_data → BUTTON_DATA_INVALID (>64 байта)
    _SUGGEST_COUNTRIES = [
        ("any", "🎲 Любая"),
        ("us",  "🇺🇸 США"),
        ("ru",  "🇷🇺 Россия"),
        ("kr",  "🇰🇷 Корея"),
        ("jp",  "🇯🇵 Япония"),
        ("gb",  "🇬🇧 Великобритания"),
        ("es",  "🇪🇸 Испания"),
        ("fr",  "🇫🇷 Франция"),
        ("de",  "🇩🇪 Германия"),
        ("it",  "🇮🇹 Италия"),
        ("tr",  "🇹🇷 Турция"),
        ("cn",  "🇨🇳 Китай"),
    ]
    _COUNTRY_LABEL = dict(_SUGGEST_COUNTRIES)
    # slug → имя страны для KP API (Кинопоиск ждёт русское название)
    _COUNTRY_SLUG_TO_KP = {
        "us": "США", "ru": "Россия", "kr": "Корея Южная", "jp": "Япония",
        "gb": "Великобритания", "es": "Испания", "fr": "Франция",
        "de": "Германия", "it": "Италия", "tr": "Турция", "cn": "Китай",
    }

    _SUGGEST_YEARS = [
        ("any",       "🎲 Любой год"),
        ("2024_2030", "🆕 Новинки 2024+"),
        ("2020_2030", "📅 2020-е"),
        ("2010_2019", "📅 2010–2019"),
        ("2000_2009", "📅 2000-е"),
        ("1990_1999", "📅 90-е"),
        ("1900_1989", "📼 Ретро (до 90-х)"),
        ("custom",    "✏️ Указать диапазон"),
    ]
    _TYPE_LABEL = dict(_SUGGEST_TYPES)
    _GENRE_LABEL = dict(_SUGGEST_GENRES)
    _YEAR_LABEL = dict(_SUGGEST_YEARS)

    class SuggestYearFSM(StatesGroup):
        waiting = State()

    # In-memory история подборок per-user (НЕ persistent — переживать рестарт
    # не требуется, это live-сессия выбора).
    # user_id → {"batches": [{"type":..., "genre":..., "year":..., "titles":[...],
    #            "message_ids":[...]}], "cursor": int}
    _suggest_history: dict[int, dict] = {}

    @router.message(Command("suggest"))
    async def cmd_suggest(message: Message) -> None:
        if not groq:
            await message.answer("🤖 Подбор от ИИ недоступен — нет GROQ_API_KEY")
            return
        await message.answer(
            "✨ <b>Что вам подобрать?</b>\n\n1️⃣ <b>Тип контента:</b>",
            parse_mode="HTML",
            reply_markup=_kb_step1_type(),
        )

    @router.message(F.text == "✨ Подобрать")
    async def btn_suggest(message: Message) -> None:
        await cmd_suggest(message)

    # ============== /blacklist — жанры которые никогда не предлагать ==============
    async def _render_blacklist(message_or_call, edit: bool = False) -> None:
        if isinstance(message_or_call, CallbackQuery):
            user_id = message_or_call.from_user.id
            chat_id = message_or_call.message.chat.id
            bot_obj = message_or_call.bot
            from_user = message_or_call.from_user
        else:
            user_id = message_or_call.from_user.id
            chat_id = message_or_call.chat.id
            bot_obj = message_or_call.bot
            from_user = message_or_call.from_user

        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=user_id, username=from_user.username, full_name=from_user.full_name,
            )
            blocked = set(await repo.list_blacklisted_genres(session, user))

        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        rows: list[list] = []
        cur: list = []
        # Идём по всем жанрам кроме «Любой жанр»
        for slug, label in _SUGGEST_GENRES[1:]:
            mark = "🚫" if slug in blocked else "✅"
            cur.append(IKB(text=f"{mark} {label}", callback_data=f"bl:{slug}"))
            if len(cur) >= 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)

        text_lines = ["🚫 <b>Жанры в чёрном списке</b>", ""]
        if blocked:
            text_lines.append(f"<b>Не предлагаются:</b> {', '.join(sorted(blocked))}")
        else:
            text_lines.append("Чёрный список пуст — ИИ может предложить любой жанр.")
        text_lines.append("")
        text_lines.append("<i>🚫 — заблокирован, ✅ — разрешён. Жми чтобы переключить.</i>")
        markup = IKM(inline_keyboard=rows)
        if edit and isinstance(message_or_call, CallbackQuery):
            try:
                await message_or_call.message.edit_text(
                    "\n".join(text_lines), parse_mode="HTML", reply_markup=markup,
                )
                return
            except Exception:
                pass
        await bot_obj.send_message(chat_id, "\n".join(text_lines), parse_mode="HTML", reply_markup=markup)

    @router.message(Command("blacklist"))
    async def cmd_blacklist(message: Message) -> None:
        await _render_blacklist(message)

    @router.callback_query(F.data.startswith("bl:"))
    async def cb_blacklist_toggle(call: CallbackQuery) -> None:
        slug = call.data.split(":", 1)[1]
        if slug not in _GENRE_LABEL:
            await call.answer("Неизвестный жанр")
            return
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            new_state = await repo.toggle_blacklisted_genre(session, user, slug)
            await session.commit()
        await call.answer("🚫 Заблокирован" if new_state else "✅ Разрешён")
        await _render_blacklist(call, edit=True)

    # ----- helpers рендера каждого шага (через edit_text одного сообщения) -----
    async def _kb_step2_genre(tg_user_id: int, type_slug: str):
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        async with session_factory() as session:
            user = await repo.get_or_create_user(session, tg_id=tg_user_id, username=None, full_name=None)
            blocked = set(await repo.list_blacklisted_genres(session, user))
        rows: list[list] = []
        cur: list = []
        for slug, label in _SUGGEST_GENRES:
            if slug != "any" and slug in blocked:
                continue
            cur.append(IKB(text=label, callback_data=f"sg:g:{type_slug}:{slug}"))
            if len(cur) >= 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        rows.append([IKB(text="⬅️ Назад", callback_data="sg:back:1")])
        return IKM(inline_keyboard=rows), blocked

    def _kb_step3_year(type_slug: str, genre_slug: str):
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        rows: list[list] = []
        cur: list = []
        for slug, label in _SUGGEST_YEARS:
            cur.append(IKB(text=label, callback_data=f"sg:y:{type_slug}:{genre_slug}:{slug}"))
            if len(cur) >= 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        rows.append([IKB(text="⬅️ Назад", callback_data=f"sg:back:2:{type_slug}")])
        return IKM(inline_keyboard=rows)

    def _kb_step4_country(type_slug: str, genre_slug: str, year_slug: str):
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        rows: list[list] = []
        cur: list = []
        for slug, label in _SUGGEST_COUNTRIES:
            cur.append(IKB(text=label, callback_data=f"sg:c:{type_slug}:{genre_slug}:{year_slug}:{slug}"))
            if len(cur) >= 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        rows.append([IKB(text="⬅️ Назад", callback_data=f"sg:back:3:{type_slug}:{genre_slug}")])
        return IKM(inline_keyboard=rows)

    def _kb_step1_type():
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        rows: list[list] = []
        cur: list = []
        for slug, label in _SUGGEST_TYPES:
            cur.append(IKB(text=label, callback_data=f"sg:t:{slug}"))
            if len(cur) >= 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        rows.append([IKB(text="🎁 Чистый рандом — удиви", callback_data="sg:t:any:any")])
        return IKM(inline_keyboard=rows)

    @router.callback_query(F.data.startswith("sg:t:"))
    async def cb_suggest_type(call: CallbackQuery) -> None:
        parts = call.data.split(":")
        type_slug = parts[2]
        # «Чистый рандом» — удаляем wizard и сразу к результату
        if len(parts) >= 4 and parts[3] == "any":
            await call.answer()
            try:
                await call.message.delete()
            except Exception:
                pass
            await _run_suggest(call.bot, call.message.chat.id, call.from_user.id, type_slug, "any", "any")
            return
        if type_slug not in _TYPE_LABEL:
            await call.answer("Неизвестный тип")
            return
        await call.answer()
        kb, blocked = await _kb_step2_genre(call.from_user.id, type_slug)
        header = f"✨ Подбор: <b>{_TYPE_LABEL[type_slug]}</b>\n\n2️⃣ <b>Жанр?</b>"
        if blocked:
            blocked_labels = ", ".join(_GENRE_LABEL.get(b, b) for b in sorted(blocked))
            header += f"\n\n<i>🚫 Скрыты из чёрного списка: {blocked_labels}</i>"
        try:
            await call.message.edit_text(header, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await call.message.answer(header, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith("sg:g:"))
    async def cb_suggest_genre(call: CallbackQuery) -> None:
        _, _, type_slug, genre_slug = call.data.split(":")
        await call.answer()
        kb = _kb_step3_year(type_slug, genre_slug)
        text = (
            f"✨ Подбор: <b>{_TYPE_LABEL.get(type_slug, '🎲')}</b> · "
            f"<b>{_GENRE_LABEL.get(genre_slug, '🎲')}</b>\n\n"
            "3️⃣ <b>Год выпуска?</b>"
        )
        try:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await call.message.answer(text, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith("sg:y:"))
    async def cb_suggest_year(call: CallbackQuery, state: FSMContext) -> None:
        _, _, type_slug, genre_slug, year_slug = call.data.split(":")
        if year_slug == "custom":
            await state.set_state(SuggestYearFSM.waiting)
            await state.update_data(type_slug=type_slug, genre_slug=genre_slug)
            await call.answer()
            try:
                await call.message.edit_text(
                    "✏️ Введи диапазон годов через дефис.\n"
                    "Например: <code>2015-2024</code>\n/cancel — отменить",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return
        await call.answer()
        kb = _kb_step4_country(type_slug, genre_slug, year_slug)
        text = (
            f"✨ Подбор: <b>{_TYPE_LABEL.get(type_slug, '🎲')}</b> · "
            f"<b>{_GENRE_LABEL.get(genre_slug, '🎲')}</b> · "
            f"<b>{_YEAR_LABEL.get(year_slug, '🎲')}</b>\n\n"
            "4️⃣ <b>Страна производства?</b>"
        )
        try:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await call.message.answer(text, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith("sg:c:"))
    async def cb_suggest_country(call: CallbackQuery) -> None:
        _, _, type_slug, genre_slug, year_slug, country_slug = call.data.split(":")
        await call.answer()
        try:
            await call.message.delete()
        except Exception:
            pass
        await _run_suggest(
            call.bot, call.message.chat.id, call.from_user.id,
            type_slug, genre_slug, year_slug, country_slug,
        )

    # «⬅️ Назад» — редактирует тот же месседж в предыдущий шаг
    @router.callback_query(F.data.startswith("sg:back:"))
    async def cb_suggest_back(call: CallbackQuery) -> None:
        parts = call.data.split(":")
        target = parts[2]
        await call.answer()
        if target == "1":
            try:
                await call.message.edit_text(
                    "✨ <b>Что вам подобрать?</b>\n\n1️⃣ <b>Тип контента:</b>",
                    parse_mode="HTML",
                    reply_markup=_kb_step1_type(),
                )
            except Exception:
                pass
            return
        if target == "2" and len(parts) >= 4:
            type_slug = parts[3]
            kb, blocked = await _kb_step2_genre(call.from_user.id, type_slug)
            header = f"✨ Подбор: <b>{_TYPE_LABEL.get(type_slug, '🎲')}</b>\n\n2️⃣ <b>Жанр?</b>"
            if blocked:
                blocked_labels = ", ".join(_GENRE_LABEL.get(b, b) for b in sorted(blocked))
                header += f"\n\n<i>🚫 Скрыты из чёрного списка: {blocked_labels}</i>"
            try:
                await call.message.edit_text(header, parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass
            return
        if target == "3" and len(parts) >= 5:
            type_slug, genre_slug = parts[3], parts[4]
            kb = _kb_step3_year(type_slug, genre_slug)
            text = (
                f"✨ Подбор: <b>{_TYPE_LABEL.get(type_slug, '🎲')}</b> · "
                f"<b>{_GENRE_LABEL.get(genre_slug, '🎲')}</b>\n\n"
                "3️⃣ <b>Год выпуска?</b>"
            )
            try:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass

    @router.message(SuggestYearFSM.waiting, Command("cancel"))
    async def suggest_year_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.")

    @router.message(SuggestYearFSM.waiting)
    async def suggest_year_typed(message: Message, state: FSMContext) -> None:
        m = re.match(r"^\s*(\d{4})\s*[-–—]\s*(\d{4})\s*$", message.text or "")
        if not m:
            await message.answer(
                "Странный ввод. Нужно вроде <code>2015-2024</code>. Попробуй ещё или /cancel",
                parse_mode="HTML",
            )
            return
        y1, y2 = int(m.group(1)), int(m.group(2))
        if y1 > y2:
            y1, y2 = y2, y1
        if y1 < 1900 or y2 > 2035:
            await message.answer("Годы 1900–2035, попробуй ещё.")
            return
        data = await state.get_data()
        await state.clear()
        year_slug = f"{y1}_{y2}"
        _YEAR_LABEL[year_slug] = f"📅 {y1}–{y2}"
        # После custom year — шлём шаг 4 (страна) новым сообщением
        type_slug = data.get("type_slug", "any")
        genre_slug = data.get("genre_slug", "any")
        kb = _kb_step4_country(type_slug, genre_slug, year_slug)
        text = (
            f"✨ Подбор: <b>{_TYPE_LABEL.get(type_slug, '🎲')}</b> · "
            f"<b>{_GENRE_LABEL.get(genre_slug, '🎲')}</b> · "
            f"<b>{_YEAR_LABEL[year_slug]}</b>\n\n"
            "4️⃣ <b>Страна производства?</b>"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

    async def _delete_prev_suggest(bot: Bot, chat_id: int, tg_user_id: int) -> None:
        """Удаляет сообщения с прошлой подборкой если они помечены."""
        sess = _suggest_history.get(tg_user_id)
        if not sess or sess["cursor"] < 0:
            return
        prev_msg_ids = sess["batches"][sess["cursor"]].get("message_ids", [])
        for mid in prev_msg_ids:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass

    async def _run_suggest(
        bot: Bot, chat_id: int, tg_user_id: int,
        type_slug: str, genre_slug: str, year_slug: str = "any",
        country_slug: str = "any",
        *, append_history: bool = True,
    ) -> None:
        if not groq:
            await bot.send_message(chat_id, "🤖 Подбор от ИИ недоступен")
            return
        type_label = _TYPE_LABEL.get(type_slug, "🎲 Любой")
        genre_label = _GENRE_LABEL.get(genre_slug, "🎲 Любой жанр")
        year_label = _YEAR_LABEL.get(year_slug, "🎲 Любой год")
        country_label = _COUNTRY_LABEL.get(country_slug, "🎲 Любая")
        year_from = year_to = None
        if year_slug != "any" and "_" in year_slug:
            try:
                year_from, year_to = (int(p) for p in year_slug.split("_"))
            except Exception:
                pass
        # slug (us/ru/...) → имя для KP API; back-compat: если пришло уже имя
        country_arg = (
            None if country_slug == "any"
            else _COUNTRY_SLUG_TO_KP.get(country_slug, country_slug)
        )

        # «Думаю» — тематический плейсхолдер
        loading_msg_id = await start_loading(bot, chat_id, context="suggest")
        await bot.send_chat_action(chat_id, action="typing")

        async with session_factory() as session:
            user = await repo.get_or_create_user(session, tg_id=tg_user_id, username=None, full_name=None)
            partner_ids: list[int] = []
            if user.pair_id:
                members = await repo.get_pair_members(session, user.pair_id)
                partner_ids = [m.id for m in members if m.id != user.id]
            my_rows = await repo.list_user_series(session, user.id, status=None)
            partner_rows: list = []
            if partner_ids:
                partner_rows = await repo.list_user_series(session, partner_ids[0], status=None)
            forbidden_genres = await repo.list_blacklisted_genres(session, user)

        all_rows = my_rows + partner_rows
        liked = sorted({s.title_ru for us, s in all_rows if us.status in ("want", "watching", "watched", "want_rewatch")})
        disliked = sorted({s.title_ru for us, s in all_rows if us.status == "dropped"})
        known_titles = {s.title_ru for _, s in all_rows}

        # Исключаем уже показанные в этой сессии — чтобы «Дальше» давала новое
        sess = _suggest_history.setdefault(tg_user_id, {"batches": [], "cursor": -1})
        for b in sess["batches"]:
            for t in b.get("titles", []):
                known_titles.add(t)

        content_type_arg = None if type_slug == "any" else type_slug
        genre_arg = None if genre_slug == "any" else genre_slug

        try:
            suggestions = await groq.suggest_for_pair(
                likes_a=liked, likes_b=[],
                dislikes_a=disliked, dislikes_b=[],
                already_in_queue=sorted(known_titles),
                content_type=content_type_arg,
                genre=genre_arg,
                year_from=year_from,
                year_to=year_to,
                forbidden_genres=forbidden_genres or None,
                country=country_arg,
                count=5,
            )
        except Exception as e:
            logger.exception("Groq suggest failed")
            await stop_loading(bot, chat_id, loading_msg_id)
            await bot.send_message(chat_id, f"😕 ИИ заглох: {e}")
            return

        # Удаляем «думаю…» и прошлый batch (если есть)
        await stop_loading(bot, chat_id, loading_msg_id)
        await _delete_prev_suggest(bot, chat_id, tg_user_id)

        if not suggestions:
            await bot.send_message(
                chat_id,
                "🤔 ИИ устал — нечего нового предложить с этими фильтрами.\n"
                "Попробуй ослабить требования или начать сначала: /suggest",
            )
            return

        header_bits = ["✨ <b>Идеи от ИИ"]
        meta_bits = []
        if content_type_arg:
            meta_bits.append(_TYPE_LABEL[type_slug])
        if genre_arg:
            meta_bits.append(_GENRE_LABEL[genre_slug])
        if year_from or year_to:
            meta_bits.append(year_label)
        if country_arg:
            meta_bits.append(country_label)
        if meta_bits:
            header_bits.append(" · " + " · ".join(meta_bits))
        # Номер batch (для понимания «дальше N+1»)
        batch_num = sess["cursor"] + (2 if append_history else 1)
        header_bits.append(f" · стр.{batch_num} ({len(suggestions)}):</b>")

        # Кнопки навигации внизу
        from aiogram.types import InlineKeyboardButton as IKB
        nav_row: list = []
        if append_history and sess["cursor"] >= 0:
            nav_row.append(IKB(text="⬅️ Назад", callback_data="sgnav:prev"))
        elif (not append_history) and sess["cursor"] > 0:
            nav_row.append(IKB(text="⬅️ Назад", callback_data="sgnav:prev"))
        nav_row.append(IKB(text="🔄 Ещё 5 →", callback_data="sgnav:next"))
        nav_kb = [nav_row, [IKB(text="🎚 Новые фильтры", callback_data="sgnav:restart")]]

        msg_ids, _ = await send_suggestions_gallery(
            bot, chat_id, suggestions, kp,
            header="".join(header_bits), extra_kb=nav_kb,
        )

        # Сохраняем в историю
        new_batch = {
            "type": type_slug,
            "genre": genre_slug,
            "year": year_slug,
            "country": country_slug,
            "titles": [s.title for s in suggestions],
            "message_ids": msg_ids,
        }
        if append_history:
            # Срезаем «forward» если юзер был не на хвосте (после Назад)
            sess["batches"] = sess["batches"][: sess["cursor"] + 1]
            sess["batches"].append(new_batch)
            sess["cursor"] = len(sess["batches"]) - 1
        else:
            # Это переход назад/перерендер — заменяем текущий
            sess["batches"][sess["cursor"]] = new_batch

    @router.callback_query(F.data.startswith("sgnav:"))
    async def cb_suggest_nav(call: CallbackQuery) -> None:
        action = call.data.split(":")[1]
        sess = _suggest_history.get(call.from_user.id)
        if not sess or not sess.get("batches"):
            await call.answer("Подборки нет. Начни заново: /suggest", show_alert=True)
            return
        if action == "restart":
            await call.answer()
            # Очищаем историю и предложение, заводим заново
            await _delete_prev_suggest(call.bot, call.message.chat.id, call.from_user.id)
            _suggest_history.pop(call.from_user.id, None)
            await cmd_suggest(call.message)
            return
        if action == "next":
            current = sess["batches"][sess["cursor"]]
            await call.answer("🔄 Подбираю ещё…")
            await _run_suggest(
                call.bot, call.message.chat.id, call.from_user.id,
                current["type"], current["genre"], current["year"],
                current.get("country", "any"),
                append_history=True,
            )
            return
        if action == "prev":
            if sess["cursor"] <= 0:
                await call.answer("Это первая подборка", show_alert=True)
                return
            await call.answer("⬅️ Возвращаюсь")
            # Удаляем текущий batch (сообщения)
            await _delete_prev_suggest(call.bot, call.message.chat.id, call.from_user.id)
            # Удаляем текущий batch из истории (мы возвращаемся «налево»)
            sess["batches"].pop(sess["cursor"])
            sess["cursor"] -= 1
            # Перерендерим предыдущий
            prev = sess["batches"][sess["cursor"]]
            # Удаляем его и из истории и заново показываем (чтоб message_ids обновились)
            sess["batches"].pop(sess["cursor"])
            sess["cursor"] -= 1
            await _run_suggest(
                call.bot, call.message.chat.id, call.from_user.id,
                prev["type"], prev["genre"], prev["year"], prev.get("country", "any"),
                append_history=True,
            )

    # ============== /swipe — Tinder для новых сериалов ==============
    @router.message(Command("swipe"))
    async def cmd_swipe(message: Message, state: FSMContext) -> None:
        loader = await start_loading(message.bot, message.chat.id, context="suggest")
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            # Что уже знаем (мы + партнёр) — не показывать
            my_rows = await repo.list_user_series(session, user.id, status=None)
            partner_rows: list = []
            if user.pair_id:
                members = await repo.get_pair_members(session, user.pair_id)
                for m in members:
                    if m.id != user.id:
                        partner_rows.extend(await repo.list_user_series(session, m.id, status=None))
            known_kp_ids = {s.kp_id for _, s in (my_rows + partner_rows)}

            # Достаём топ-жанры из лайков для персонализации
            genre_counter: Counter = Counter()
            for us, s in my_rows + partner_rows:
                if us.status != "dropped" and s.genres:
                    for g in s.genres.split(","):
                        g = g.strip()
                        if g:
                            genre_counter[g] += 1
            top_genres = [g for g, _ in genre_counter.most_common(3)]
            blocked = {g.lower() for g in await repo.list_blacklisted_genres(session, user)}

        def _swipe_ok(h) -> bool:
            if h.kp_id in known_kp_ids or not h.poster_url:
                return False
            if blocked and any(g.lower() in blocked for g in (h.genres or [])):
                return False
            return True

        # Тянем свежие сериалы из KP — сначала с подходящими жанрами, потом без
        candidates: list = []
        try:
            if top_genres:
                hits = await kp.get_upcoming_series(genres=top_genres, limit=25)
                candidates.extend(h for h in hits if _swipe_ok(h))
            if len(candidates) < 5:
                hits = await kp.get_upcoming_series(limit=25)
                for h in hits:
                    if _swipe_ok(h) and h.kp_id not in {c.kp_id for c in candidates}:
                        candidates.append(h)
        except Exception as e:
            logger.warning("kp.get_upcoming_series failed: %s", e)

        await stop_loading(message.bot, message.chat.id, loader)
        if not candidates:
            await message.answer(
                "🃏 Не нашёл новых сериалов для свайпа. Попробуй позже или используй /suggest.",
            )
            return

        random.shuffle(candidates)
        queue = [(c.kp_id, c.title_ru) for c in candidates[:10]]
        await state.update_data(queue=queue, idx=0)
        await state.set_state(SwipeFSM.swiping)
        genres_hint = f" с упором на: {', '.join(top_genres)}" if top_genres else ""
        await message.answer(
            f"🃏 <b>Свайп-вечер</b>\n\n"
            f"Покажу {len(queue)} свежих сериалов{genres_hint}.\n"
            f"❤️ — добавить в «👀 Хочу», 👎 — пропустить.",
            parse_mode="HTML",
        )
        await _send_swipe_card_by_kp(message.bot, message.chat.id, queue[0][0], 0)

    @router.callback_query(SwipeFSM.swiping, F.data.startswith("sw:"))
    async def cb_swipe(call: CallbackQuery, state: FSMContext) -> None:
        _, action, kp_id_s, queue_idx_s = call.data.split(":")
        queue_idx = int(queue_idx_s)
        data = await state.get_data()
        queue: list = data.get("queue", [])
        if action == "stop":
            await state.clear()
            await call.message.answer("🏁 Свайп окончен. Что отметил «хочу» — теперь в /list 👀")
            await call.answer()
            return
        if action == "yes":
            kp_id = int(kp_id_s)
            await _add_by_kp_id(
                call.bot, call.message.chat.id, call.from_user.id, kp_id,
                session_factory, kp, silent=True,
            )
            await call.answer("👀 Добавил в «Хочу»!")
        else:
            await call.answer("👎 Скип")
        next_idx = queue_idx + 1
        if next_idx >= len(queue):
            await state.clear()
            await call.message.answer("🏁 Все варианты показал! Открой /list 👀")
            return
        await state.update_data(idx=next_idx)
        await _send_swipe_card_by_kp(call.bot, call.message.chat.id, queue[next_idx][0], next_idx)

    async def _send_swipe_card_by_kp(bot: Bot, chat_id: int, kp_id: int, idx: int) -> None:
        """Свайп-карточка по kp_id — не нужно сначала добавлять в БД."""
        try:
            details = await kp.get_details(kp_id)
        except Exception as e:
            logger.warning("KP details failed for swipe %s: %s", kp_id, e)
            return
        rating_bits = []
        if details.rating_kp:
            rating_bits.append(f"⭐ КП {details.rating_kp:.1f}")
        if details.rating_imdb:
            rating_bits.append(f"IMDb {details.rating_imdb:.1f}")
        desc = (details.description_ru or "")[:400]
        if details.description_ru and len(details.description_ru) > 400:
            desc += "…"
        lines = [
            f"#{idx + 1} · 🎬 <b>{details.title_ru}</b>",
            f"({details.year})" if details.year else "",
            " · ".join(rating_bits),
            f"🎭 {', '.join(details.genres[:4])}" if details.genres else "",
            "",
            desc,
        ]
        caption = "\n".join(l for l in lines if l)
        # swipe_keyboard принимает series_id — но мы пока без БД, шлём kp_id
        kb = swipe_keyboard(kp_id, idx)
        if details.poster_url:
            try:
                await bot.send_photo(
                    chat_id=chat_id, photo=details.poster_url,
                    caption=caption, parse_mode="HTML", reply_markup=kb,
                )
                return
            except Exception:
                pass
        await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)

    # ============== /checkin manual ==============
    @router.message(Command("checkin"))
    async def cmd_checkin_manual(message: Message) -> None:
        sent = await run_weekly_checkin(message.bot, session_factory)
        await message.answer(f"🔔 Опрос отправлен: {sent} сериал(ов).")



    # ============== Открыть карточку из компактного списка ==============
    @router.callback_query(F.data.startswith("open:"))
    async def cb_open(call: CallbackQuery) -> None:
        series_id = int(call.data.split(":")[1])
        async with session_factory() as session:
            series = await session.get(Series, series_id)
            if series is None:
                await call.answer("Сериал не найден")
                return
            us = await repo.get_user_series(session, call.from_user.id, series_id)
        await call.answer()
        await _send_card(
            call.bot, call.message.chat.id, series,
            user_status=us.status if us else None,
            user_rating=us.rating if us else None,
            note=us.notes if us else None,
            notify_releases=bool(us and us.notify_releases),
            progress=us.current_episode if us else None,
        )

    @router.callback_query(F.data.startswith("open_random:"))
    async def cb_open_random(call: CallbackQuery) -> None:
        status = call.data.split(":")[1]
        async with session_factory() as session:
            rows = await repo.list_user_series(session, call.from_user.id, status=status)
        if not rows:
            await call.answer("Список пуст")
            return
        us, series = random.choice(rows)
        await call.answer("Вот случайный 🎲")
        await _send_card(
            call.bot, call.message.chat.id, series,
            user_status=us.status, user_rating=us.rating, note=us.notes,
            notify_releases=bool(us.notify_releases),
            progress=us.current_episode,
        )

    # ============== Bulk-перевод статусов ==============
    @router.callback_query(F.data.startswith("bulk:"))
    async def cb_bulk(call: CallbackQuery) -> None:
        _, from_status, to_status = call.data.split(":")
        async with session_factory() as session:
            await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            n = await repo.bulk_set_status(session, call.from_user.id, from_status, to_status)
            await session.commit()
        await call.answer(f"✅ Переведено: {n}")
        await call.message.answer(
            f"✅ Перевёл {n} сериалов в <b>{STATUS_LABELS.get(to_status, to_status)}</b>",
            parse_mode="HTML",
        )

    # ============== Текст без префикса /add = поиск ==============
    # Список текстов reply-кнопок — их игнорим (обрабатываются своими хендлерами)
    _BUTTON_TEXTS = {
        "🎬 Добавить", "🎲 Что включить?",
        "👀 Хочу", "▶️ Смотрю",
        "✅ Посмотрел", "🔁 Пересмотреть",
        "💛 Лайки оба", "✨ Подобрать",
        "👤 Профиль", "🔥 Новинки", "💛 Поддержать",
        "👫 Пара", "📂 Меню",
        "📊 Статистика", "ℹ️ Помощь",
    }

    # Триггеры для Groq-интерпретации. Любая фраза похожая на команду/
    # вопрос/рассуждение — идёт в ИИ. Короткие тайтлы — через прямой поиск.
    _COMMAND_HINTS = (
        # Управление
        "исключ", "убер", "не предла", "не показывай", "забан", "блок",
        "добавь в чёрн", "добавь в черн", "в чёрный", "в черный",
        "верни", "сними", "разреши",
        # Показать
        "покажи", "список", "посмотреть", "что в", "что есть",
        "что мы", "что я", "афиш",
        # Поиск
        "найди", "трейлер", "где смотр", "где посмотр",
        # Подбор
        "подбери", "подбор", "подскажи", "посоветуй", "посоветуйте",
        # YouTube
        "подпиш", "подписаться",
        # Вопросительные/диалоговые слова
        "что ", "какой", "какая", "какое", "какие", "когда", "почему", "зачем",
        "стоит ли", "годный", "норм", "хорош",
    )

    def _looks_like_command(text: str) -> bool:
        t = text.lower().strip()
        if "?" in t:
            return True
        if any(h in t for h in _COMMAND_HINTS):
            return True
        # Любая фраза 5+ слов — скорее всего разговор, а не тайтл
        if len(t.split()) >= 5:
            return True
        return False

    # Локальная эвристика для типичных команд — быстрее Groq на ~1-2 сек.
    _QUICK_PATTERNS = (
        (("покажи список", "мои фильмы", "мой список", "что хочу", "хочу посмотреть"), "show_list"),
        (("что смотрю", "сейчас смотрю", "что смотрим"), "show_watching"),
        (("что досмотрел", "досмотренное", "что мы досмотрели"), "show_watched"),
        (("моя статистика", "стата", "сколько у меня"), "show_stats"),
        (("что в кино", "афиша", "сегодня в кино"), "show_cinema"),
        (("новинки", "что новенького", "новое"), "show_upcoming"),
        (("мой профиль", "профиль", "настройки"), "show_profile"),
        (("чёрный список", "черный список", "blacklist"), "show_blacklist"),
        (("громкие новинки", "трендинг", "trending"), "show_trending"),
    )

    def _try_quick_intent(text: str) -> Optional[str]:
        t = text.lower().strip()
        for phrases, action in _QUICK_PATTERNS:
            if any(p in t for p in phrases):
                return action
        return None

    @router.message(F.text & ~F.text.startswith("/"))
    async def text_as_search(message: Message, state: FSMContext) -> None:
        current_state = await state.get_state()
        if current_state is not None:
            return
        text = (message.text or "").strip()
        if not text or text in _BUTTON_TEXTS:
            return
        if len(text) < 2:
            return
        # Несколько названий одним сообщением → bulk
        bulk = _parse_titles_bulk(text)
        if bulk:
            await _bulk_add_titles(message.bot, message.chat.id, message.from_user.id, bulk)
            return
        # Локальная эвристика — обрабатываем мгновенно без Groq
        quick = _try_quick_intent(text)
        if quick:
            handled = await _handle_intent(message, {"action": quick})
            if handled:
                return
        # Текст похож на сложную команду — спрашиваем Groq
        if groq and _looks_like_command(text):
            loader = await start_loading(message.bot, message.chat.id, context="search")
            try:
                intent = await groq.interpret_command(text)
            finally:
                await stop_loading(message.bot, message.chat.id, loader)
            handled = await _handle_intent(message, intent)
            if handled:
                return
        await _do_search_and_show(message.bot, message.chat.id, message.from_user.id, text, state=state)

    async def _handle_intent(message: Message, intent: dict) -> bool:
        """Возвращает True если intent обработан и поиск делать не надо."""
        action = (intent or {}).get("action", "")
        if action == "blacklist_add":
            genre = (intent.get("genre") or "").strip().lower()
            if genre not in _GENRE_LABEL:
                await message.answer(
                    f"🤔 Не понял жанр «{genre}». Доступные: {', '.join(g for g, _ in _SUGGEST_GENRES[1:])}\n"
                    f"Или открой /blacklist — там список с кнопками.",
                )
                return True
            async with session_factory() as session:
                user = await repo.get_or_create_user(
                    session, tg_id=message.from_user.id,
                    username=message.from_user.username, full_name=message.from_user.full_name,
                )
                # Чтобы не toggle ↔ — добавляем только если ещё нет
                current = set(await repo.list_blacklisted_genres(session, user))
                if genre not in current:
                    await repo.toggle_blacklisted_genre(session, user, genre)
                    await session.commit()
            await message.answer(
                f"🚫 <b>{_GENRE_LABEL.get(genre, genre)}</b> — в чёрном списке. "
                f"Больше не предложу.",
                parse_mode="HTML",
            )
            return True

        if action == "blacklist_remove":
            genre = (intent.get("genre") or "").strip().lower()
            async with session_factory() as session:
                user = await repo.get_or_create_user(
                    session, tg_id=message.from_user.id,
                    username=message.from_user.username, full_name=message.from_user.full_name,
                )
                current = set(await repo.list_blacklisted_genres(session, user))
                if genre in current:
                    await repo.toggle_blacklisted_genre(session, user, genre)
                    await session.commit()
                    await message.answer(
                        f"✅ <b>{_GENRE_LABEL.get(genre, genre)}</b> — снова разрешён.",
                        parse_mode="HTML",
                    )
                else:
                    await message.answer(f"💡 «{genre}» и так не в чёрном списке.")
            return True

        if action == "show_blacklist":
            await cmd_blacklist(message)
            return True
        if action == "show_list":
            await cmd_list(message)
            return True
        if action == "show_watching":
            await cmd_watching(message)
            return True
        if action == "show_watched":
            await cmd_watched(message)
            return True
        if action == "show_stats":
            await cmd_stats(message)
            return True
        if action == "show_cinema":
            await cmd_cinema(message)
            return True
        if action == "show_upcoming":
            await cmd_upcoming(message)
            return True
        if action == "show_profile":
            await cmd_profile(message)
            return True
        if action == "show_trending":
            await cmd_trending(message)
            return True
        if action == "show_subs":
            # Команда из start.py — отправляем заглушку
            await message.answer("📺 Открой /subs — там список твоих YouTube подписок.")
            return True
        if action == "find_trailer":
            title = (intent.get("title") or "").strip()
            if title:
                await message.answer(
                    f"🔎 Ищу <b>{title}</b> — найду карточку, потом жми «🎥 Трейлер».",
                    parse_mode="HTML",
                )
                await _do_search_and_show(message.bot, message.chat.id, message.from_user.id, title)
            return True
        if action == "where_for_title":
            title = (intent.get("title") or "").strip()
            if not title:
                return False
            async with session_factory() as session:
                user = await repo.get_or_create_user(
                    session, tg_id=message.from_user.id,
                    username=message.from_user.username, full_name=message.from_user.full_name,
                )
                rows = await repo.list_user_series(session, user.id, status=None)
            tl = title.lower()
            matched = [s for _, s in rows if tl in (s.title_ru or "").lower() or tl in (s.title_en or "").lower()]
            if matched:
                await _show_where_for_series(message.bot, message.chat.id, matched[0])
            else:
                await message.answer(
                    f"🤷 <b>{title}</b> нет в твоих списках. Сначала добавь через /add.",
                    parse_mode="HTML",
                )
            return True
        if action == "suggest":
            ct = intent.get("content_type") or "any"
            gn = (intent.get("genre") or "any").lower()
            if gn not in _GENRE_LABEL:
                gn = "any"
            if ct not in _TYPE_LABEL:
                ct = "any"
            year_slug = "any"
            yf, yt = intent.get("year_from"), intent.get("year_to")
            if isinstance(yf, int) and isinstance(yt, int):
                year_slug = f"{yf}_{yt}"
                _YEAR_LABEL[year_slug] = f"📅 {yf}–{yt}"
            country = (intent.get("country") or "any").strip()
            await _run_suggest(message.bot, message.chat.id, message.from_user.id, ct, gn, year_slug, country)
            return True
        if action == "chat":
            response = (intent.get("response") or "").strip()
            if response:
                await message.answer(response, parse_mode="HTML")
            else:
                await message.answer(
                    "🤔 Я тут чтобы помогать с сериалами. Попробуй /suggest, /upcoming, /cinema.",
                )
            return True
        if action == "search":
            title = (intent.get("title") or "").strip()
            if title:
                await _do_search_and_show(message.bot, message.chat.id, message.from_user.id, title)
                return True
        # unknown — пусть отрабатывает обычный поиск
        return False


    # ============== Inline-режим: @bot Severance в любом чате ==============
    @router.inline_query()
    async def inline_search(query: InlineQuery) -> None:
        q = (query.query or "").strip()
        if len(q) < 2:
            await query.answer([], cache_time=10, is_personal=True)
            return
        try:
            hits = await kp.search(q, limit=10)
        except Exception as e:
            logger.warning("inline KP search failed: %s", e)
            await query.answer([], cache_time=5, is_personal=True)
            return
        bot_user = await query.bot.me()
        results = []
        for h in hits[:10]:
            title = h.title_ru + (f" ({h.year})" if h.year else "")
            rating = f" ⭐ КП {h.rating_kp:.1f}" if h.rating_kp else ""
            short = (h.short_description or "")[:120]
            text = f"🎬 <b>{h.title_ru}</b>"
            if h.year:
                text += f" ({h.year})"
            if h.rating_kp:
                text += f"\n⭐ КП {h.rating_kp:.1f}"
            if h.short_description:
                text += "\n\n" + (h.short_description[:300])
            text += f"\n\n👉 <a href=\"https://t.me/{bot_user.username}?start=show_{h.kp_id}\">Открыть в боте «Диванные критики»</a>"
            from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
            add_url = f"https://t.me/{bot_user.username}?start=add_{h.kp_id}"
            results.append(
                InlineQueryResultArticle(
                    id=str(h.kp_id),
                    title=title + rating,
                    description=short,
                    thumbnail_url=h.poster_url or None,
                    input_message_content=InputTextMessageContent(
                        message_text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    ),
                    reply_markup=IKM(inline_keyboard=[
                        [IKB(text="➕ Добавить себе в список", url=add_url)],
                    ]),
                )
            )
        await query.answer(results, cache_time=60, is_personal=True)

    return router
