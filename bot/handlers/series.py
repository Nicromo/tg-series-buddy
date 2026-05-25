"""Хендлеры команд, callback-кнопок, кнопок главного меню и фото."""

from __future__ import annotations

import io
import logging
import random
from collections import Counter
from typing import Optional

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
from ..keyboards.series_kb import (
    bulk_move_keyboard,
    card_keyboard,
    checkin_keyboard,
    rating_only_keyboard,
    search_results_keyboard,
    swipe_keyboard,
    trailer_link_keyboard,
)
from ..services.groq_ai import GroqClient
from ..services.kinopoisk import KinopoiskClient, KPDetails
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
                "Или просто пришли скрин/постер — распознаю 📸",
                parse_mode="HTML",
            )
            return
        await _do_search_and_show(message.bot, message.chat.id, message.from_user.id, parts[1].strip(), state=state)

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

        await message.answer(f"🔎 Распознал: <b>{title}</b>\nИщу в Кинопоиске…", parse_mode="HTML")
        await _do_search_and_show(message.bot, message.chat.id, message.from_user.id, title, state=state)

    async def _do_search_and_show(bot: Bot, chat_id: int, tg_user_id: int, query: str, *, state: Optional[FSMContext] = None) -> None:
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

        if len(hits) == 1:
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
            us = await repo.set_user_series_status(session, call.from_user.id, series_id, status)
            await session.commit()

            # Если только что отметили "досмотрел" и нет оценки — попросим
            if status == "watched" and not us.rating:
                series = await session.get(Series, series_id)
                title = series.title_ru if series else "сериал"
                await call.message.answer(
                    f"🎯 <b>{title}</b> — досмотрено! Поставь оценку:",
                    parse_mode="HTML",
                    reply_markup=rating_only_keyboard(series_id),
                )

        await call.answer(f"✅ {STATUS_LABELS.get(status, status)}")

    @router.callback_query(F.data.startswith("rt:"))
    async def cb_rating(call: CallbackQuery) -> None:
        _, new_rating, series_id_s = call.data.split(":")
        series_id = int(series_id_s)
        cancelled = False
        async with session_factory() as session:
            await repo.get_or_create_user(
                session,
                tg_id=call.from_user.id,
                username=call.from_user.username,
                full_name=call.from_user.full_name,
            )
            current_us = await repo.get_user_series(session, call.from_user.id, series_id)
            # Toggle: повторное нажатие той же кнопки — снимает оценку
            if current_us and current_us.rating == new_rating:
                await repo.clear_user_series_rating(session, call.from_user.id, series_id)
                cancelled = True
            else:
                await repo.set_user_series_rating(session, call.from_user.id, series_id, new_rating)
            await session.commit()

        if cancelled:
            await call.answer(f"Отменил оценку {RATING_LABELS.get(new_rating, new_rating)}")
            return
        await call.answer(f"Принято: {RATING_LABELS.get(new_rating, new_rating)}")
        # После лайка — предложить похожие через Groq
        if new_rating == "like" and groq:
            from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
            await call.message.answer(
                "👍 Зашло? Подобрать похожие?",
                reply_markup=IKM(inline_keyboard=[[
                    IKB(text="🎬 Похожие сериалы", callback_data=f"simto:{series_id}")
                ]]),
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
                us = await repo.set_user_series_status(session, call.from_user.id, series_id, "watched")
                await session.commit()
                if not us.rating:
                    series = await session.get(Series, series_id)
                    title = series.title_ru if series else "сериал"
                    await call.message.answer(
                        f"🎯 <b>{title}</b> — досмотрено! Поставь оценку:",
                        parse_mode="HTML",
                        reply_markup=rating_only_keyboard(series_id),
                    )
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
            title = series.title_ru
            title_en = series.title_en
            year = series.year
            trailer_lang = series.trailer_language
            kp_id = series.kp_id

        # 1) Кешированный yt_id
        if yt_id:
            only_en = bool(trailer_lang and trailer_lang != "ru")
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

        # 2) Ничего нет в БД — поищем через все источники
        await call.bot.send_chat_action(call.message.chat.id, action="typing")
        imdb_id = tmdb_id = None
        is_series_flag = True
        try:
            details = await kp.get_details(kp_id)
            imdb_id = details.imdb_id
            tmdb_id = details.tmdb_id
            is_series_flag = details.is_series
            # KP мог обновить trailers с момента upsert'а — сразу проверим
            if details.best_trailer_youtube_id:
                yt_id = details.best_trailer_youtube_id
                trailer_lang = details.best_trailer_language
        except Exception as e:
            logger.warning("kp.get_details for trailer search failed: %s", e)

        if not yt_id:
            try:
                yt_id = await trailer_finder.find(
                    title=title, year=year, title_en=title_en,
                    imdb_id=imdb_id, tmdb_id=tmdb_id,
                    is_series=is_series_flag,
                )
            except Exception as e:
                logger.exception("trailer_finder failed: %s", e)

        if yt_id:
            # Кешируем yt_id для повторных кликов
            async with session_factory() as session:
                s = await session.get(Series, series_id)
                if s:
                    s.trailer_youtube_id = yt_id
                    if trailer_lang:
                        s.trailer_language = trailer_lang
                    await session.commit()
            only_en = bool(trailer_lang and trailer_lang != "ru")
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

        # 3) TG-канал
        tg_link = await find_trailer_tg_link(title, year)
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

        # Если KP не дал — пробуем все источники
        if not yt_id:
            try:
                yt_id = await trailer_finder.find(
                    title=title, year=year, title_en=details.title_en,
                    imdb_id=details.imdb_id, tmdb_id=details.tmdb_id,
                    is_series=details.is_series,
                )
            except Exception as e:
                logger.warning("trailer_finder failed in trkp: %s", e)

        if yt_id:
            only_en = bool(trailer_lang and trailer_lang != "ru")
            if _inline_video_enabled and await _try_send_inline_video(
                call.message.chat.id, call.bot,
                title=title, youtube_id=yt_id, only_english=only_en,
            ):
                return
            url = f"https://www.youtube.com/watch?v={yt_id}"
            await _send_trailer_message(
                call.message.chat.id, call.bot, title, url, only_english=only_en,
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
    async def _send_list(message: Message, status: str, empty_msg: str, *, header: str = "") -> None:
        async with session_factory() as session:
            rows = await repo.list_user_series(session, message.from_user.id, status=status)
        if not rows:
            await message.answer(empty_msg)
            return

        items = rows[:10]
        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM

        # Галерея постеров (без подписи — заголовок отдельным сообщением)
        media = [InputMediaPhoto(media=s.poster_url) for _, s in items if s.poster_url]
        if len(media) >= 2:
            try:
                await message.bot.send_media_group(message.chat.id, media[:10])
            except Exception as e:
                logger.warning("send_media_group failed for /%s list: %s", status, e)

        # Текст: заголовок + пронумерованный список
        title = header or "Список"
        lines = [f"{title}  ·  {len(rows)} шт.", ""]
        for i, (us, s) in enumerate(items, 1):
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            year_str = f" ({s.year})" if s.year else ""
            rating_str = f" ⭐{s.rating_kp:.1f}" if s.rating_kp else ""
            lines.append(f"{marker} <b>{s.title_ru}</b>{year_str}{rating_str}")
        if len(rows) > 10:
            lines.append("")
            lines.append(f"<i>… показал первые 10 из {len(rows)}. /find для поиска.</i>")
        lines.append("")
        lines.append("<i>👇 Жми номер чтобы открыть полную карточку</i>")

        # Кнопки 1-N (по 5 в ряд) — иконка глаза подсказывает «открыть»
        btn_rows: list[list] = []
        cur: list = []
        for i, (_, s) in enumerate(items, 1):
            cur.append(IKB(text=f"👁 {i}", callback_data=f"open:{s.id}"))
            if len(cur) >= 5:
                btn_rows.append(cur)
                cur = []
        if cur:
            btn_rows.append(cur)

        # Доп. кнопки
        action_row = []
        if status == "want":
            action_row.append(IKB(text="🎲 Случайный", callback_data="open_random:want"))
            if len(rows) >= 2:
                action_row.append(IKB(text=f"▶️ Все в смотрю ({len(rows)})", callback_data="bulk:want:watching"))
        elif status == "watching" and len(rows) >= 2:
            action_row.append(IKB(text=f"✅ Все досмотрел ({len(rows)})", callback_data="bulk:watching:watched"))
        if action_row:
            btn_rows.append(action_row)

        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=btn_rows),
            disable_web_page_preview=True,
        )

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        await _send_list(message, "want", "Очередь пустая. Добавь /add &lt;название&gt;", header="👀 <b>Хочу посмотреть:</b>")

    @router.message(Command("watching"))
    async def cmd_watching(message: Message) -> None:
        await _send_list(message, "watching", "Сейчас ничего не смотришь.", header="▶️ <b>Смотрим сейчас:</b>")

    @router.message(Command("watched"))
    async def cmd_watched(message: Message) -> None:
        await _send_list(message, "watched", "Ещё ничего не досмотрел до конца.", header="✅ <b>Досмотрено:</b>")

    @router.message(Command("rewatch"))
    async def cmd_rewatch(message: Message) -> None:
        await _send_list(message, "want_rewatch", "Список пересмотра пуст. Жми 🔁 в карточке досмотренного сериала.", header="🔁 <b>Пересмотреть:</b>")

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
        if not rows:
            await message.answer("Очередь пустая.")
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

        # Случайный из активных: watching → want → rewatch
        for status, label in [("watching", "Ты ведь это смотришь"), ("want", "Из очереди"), ("want_rewatch", "Из пересмотра")]:
            rows = [(us, s) for us, s in all_rows if us.status == status]
            if rows:
                us, series = random.choice(rows)
                await message.answer(f"🍿 <b>{label}:</b>", parse_mode="HTML")
                await _send_card(message.bot, message.chat.id, series, user_status=us.status, user_rating=us.rating, note=us.notes)
                return
        # Бывает что есть только watched/dropped — тогда ничего не показываем дополнительно
        await message.answer("Активных нет. Может что-то пересмотреть? 🔁")

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

    # ============== /match ==============
    @router.message(Command("match"))
    async def cmd_match(message: Message) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            if not user.pair_id:
                await message.answer(
                    "👫 <b>Сначала свяжись с партнёром</b>\n\n"
                    "Жми кнопку <b>👫 Пара</b> снизу (или команду /pair) — "
                    "получишь инвайт-код. Перешли его жене/мужу — они напишут "
                    "<code>/pair &lt;твой код&gt;</code>, и появятся общие лайки 💛",
                    parse_mode="HTML",
                )
                return
            matches = await repo.list_pair_matches(session, user.pair_id)
        if not matches:
            await message.answer(
                "💛 <b>Пока нет общих лайков</b>\n\n"
                "Лайкайте сериалы 👍 кнопкой под карточкой — когда вы "
                "<i>оба</i> лайкнете один и тот же, он появится здесь.",
                parse_mode="HTML",
            )
            return

        from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        items = matches[:10]

        # Галерея постеров
        media = [InputMediaPhoto(media=s.poster_url) for s in items if s.poster_url]
        if len(media) >= 2:
            try:
                await message.bot.send_media_group(message.chat.id, media[:10])
            except Exception as e:
                logger.warning("send_media_group failed for /match: %s", e)

        # Текст + кнопки
        lines = [f"💛 <b>Лайкнули оба ({len(matches)}):</b>", ""]
        for i, s in enumerate(items, 1):
            marker = DIGIT_EMOJI[i - 1] if i <= len(DIGIT_EMOJI) else f"{i}."
            year_str = f" ({s.year})" if s.year else ""
            rating_str = f" ⭐{s.rating_kp:.1f}" if s.rating_kp else ""
            lines.append(f"{marker} <b>{s.title_ru}</b>{year_str}{rating_str}")
        if len(matches) > 10:
            lines.append("")
            lines.append(f"<i>… показал первые 10 из {len(matches)}.</i>")
        lines.append("")
        lines.append("<i>👇 Жми номер чтобы открыть полную карточку</i>")

        btn_rows: list[list] = []
        cur: list = []
        for i, s in enumerate(items, 1):
            cur.append(IKB(text=f"👁 {i}", callback_data=f"open:{s.id}"))
            if len(cur) >= 5:
                btn_rows.append(cur)
                cur = []
        if cur:
            btn_rows.append(cur)
        btn_rows.append([IKB(text="🎲 Случайный из общих", callback_data="match_random:0")])

        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=IKM(inline_keyboard=btn_rows),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data.startswith("match_random:"))
    async def cb_match_random(call: CallbackQuery) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=call.from_user.id,
                username=call.from_user.username, full_name=call.from_user.full_name,
            )
            if not user.pair_id:
                await call.answer("Нет пары")
                return
            matches = await repo.list_pair_matches(session, user.pair_id)
        if not matches:
            await call.answer("Нет общих лайков")
            return
        s = random.choice(matches)
        await call.answer("Включаем! 🎬")
        await _send_card(call.bot, call.message.chat.id, s)

    @router.message(F.text == "💛 Лайки оба")
    async def btn_match(message: Message) -> None:
        await cmd_match(message)

    # ============== /stats ==============
    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        async with session_factory() as session:
            all_rows = await repo.list_user_series(session, message.from_user.id, status=None)
        if not all_rows:
            await message.answer("📊 Пока пусто. Добавь что-нибудь через /add 🎬")
            return
        st_count = Counter(us.status for us, _ in all_rows)
        likes = sum(1 for us, _ in all_rows if us.rating == "like")
        dislikes = sum(1 for us, _ in all_rows if us.rating == "dislike")
        genre_counter: Counter = Counter()
        for us, s in all_rows:
            if us.rating == "like" and s.genres:
                for g in s.genres.split(","):
                    g = g.strip()
                    if g:
                        genre_counter[g] += 1
        top_genres = ", ".join(g for g, _ in genre_counter.most_common(3)) or "—"
        text = (
            "📊 <b>Твоя статистика</b>\n\n"
            f"📺 Всего сериалов: <b>{len(all_rows)}</b>\n"
            f"👀 Хочу: {st_count.get('want', 0)}\n"
            f"▶️ Смотрю: {st_count.get('watching', 0)}\n"
            f"✅ Досмотрел: {st_count.get('watched', 0)}\n"
            f"🔁 Пересмотр: {st_count.get('want_rewatch', 0)}\n"
            f"❌ Дропнул: {st_count.get('dropped', 0)}\n\n"
            f"👍 Лайков: {likes}  ·  👎 Дизлайков: {dislikes}\n"
            f"🎭 Топ жанры: <i>{top_genres}</i>"
        )
        await message.answer(text, parse_mode="HTML")

    @router.message(F.text == "📊 Статистика")
    async def btn_stats(message: Message) -> None:
        await cmd_stats(message)

    # ============== /suggest — Groq AI рекомендации ==============
    @router.message(Command("suggest"))
    async def cmd_suggest(message: Message) -> None:
        if not groq:
            await message.answer("🤖 Подбор от ИИ недоступен — нет GROQ_API_KEY")
            return
        await message.bot.send_chat_action(message.chat.id, action="typing")
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, tg_id=message.from_user.id,
                username=message.from_user.username, full_name=message.from_user.full_name,
            )
            partner_ids: list[int] = []
            if user.pair_id:
                members = await repo.get_pair_members(session, user.pair_id)
                partner_ids = [m.id for m in members if m.id != user.id]

            my_rows = await repo.list_user_series(session, user.id, status=None)
            partner_rows: list = []
            if partner_ids:
                partner_rows = await repo.list_user_series(session, partner_ids[0], status=None)

        likes_a = [s.title_ru for us, s in my_rows if us.rating == "like"]
        dislikes_a = [s.title_ru for us, s in my_rows if us.rating == "dislike"]
        likes_b = [s.title_ru for us, s in partner_rows if us.rating == "like"]
        dislikes_b = [s.title_ru for us, s in partner_rows if us.rating == "dislike"]
        # Исключаем ВСЕ известные сериалы (включая watched/dropped/want_rewatch) —
        # «уже смотрел / уже знаком / не интересно» не должны предлагаться повторно.
        known_titles = {s.title_ru for _, s in my_rows} | {s.title_ru for _, s in partner_rows}
        queue = sorted(known_titles)

        try:
            suggestions = await groq.suggest_for_pair(
                likes_a=likes_a, likes_b=likes_b,
                dislikes_a=dislikes_a, dislikes_b=dislikes_b,
                already_in_queue=queue,
            )
        except Exception as e:
            logger.exception("Groq suggest failed")
            await message.answer(f"😕 ИИ заглох: {e}")
            return

        if not suggestions:
            await message.answer("🤔 Не получилось придумать. Попробуй позже.")
            return

        await send_suggestions_gallery(
            message.bot, message.chat.id, suggestions, kp,
            header=f"✨ <b>Идеи от ИИ ({len(suggestions)}):</b>",
        )

    @router.message(F.text == "✨ Подобрать")
    async def btn_suggest(message: Message) -> None:
        await cmd_suggest(message)

    # ============== /swipe — игровой режим ==============
    @router.message(Command("swipe"))
    async def cmd_swipe(message: Message, state: FSMContext) -> None:
        async with session_factory() as session:
            rows = await repo.list_user_series(session, message.from_user.id, status="want")
        if not rows:
            await message.answer("🃏 Очередь пустая — нечего свайпать.")
            return
        random.shuffle(rows)
        queue = [(us.series_id, s.title_ru) for us, s in rows[:10]]
        await state.update_data(queue=queue, idx=0)
        await state.set_state(SwipeFSM.swiping)
        await message.answer(f"🎮 <b>Свайп-вечер</b>\n\nПокажу {len(queue)} сериалов. ❤️ — хочу включить, 👎 — пропустить.", parse_mode="HTML")
        await _send_swipe_card(message.bot, message.chat.id, queue[0][0], 0, session_factory)

    @router.callback_query(SwipeFSM.swiping, F.data.startswith("sw:"))
    async def cb_swipe(call: CallbackQuery, state: FSMContext) -> None:
        _, action, series_id_s, queue_idx_s = call.data.split(":")
        queue_idx = int(queue_idx_s)
        data = await state.get_data()
        queue: list = data.get("queue", [])
        if action == "stop":
            await state.clear()
            await call.message.answer("🏁 Свайп окончен.")
            await call.answer()
            return
        if action == "yes":
            series_id = int(series_id_s)
            async with session_factory() as session:
                await repo.set_user_series_rating(session, call.from_user.id, series_id, "like")
                await session.commit()
            await call.answer("❤️ Лайк!")
        else:
            await call.answer("👎 Скип")
        next_idx = queue_idx + 1
        if next_idx >= len(queue):
            await state.clear()
            await call.message.answer("🏁 Все варианты показал! Глянь /match — может, что-то совпало.")
            return
        await state.update_data(idx=next_idx)
        await _send_swipe_card(call.bot, call.message.chat.id, queue[next_idx][0], next_idx, session_factory)

    async def _send_swipe_card(bot: Bot, chat_id: int, series_id: int, idx: int, sf: async_sessionmaker) -> None:
        async with sf() as session:
            series = await session.get(Series, series_id)
            if series is None:
                return
        caption = f"#{idx + 1} · {_format_caption(series)}"
        kb = swipe_keyboard(series_id, idx)
        if series.poster_url:
            try:
                await bot.send_photo(chat_id=chat_id, photo=series.poster_url, caption=caption, parse_mode="HTML", reply_markup=kb)
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
        "📊 Статистика", "ℹ️ Помощь",
    }

    @router.message(F.text & ~F.text.startswith("/"))
    async def text_as_search(message: Message, state: FSMContext) -> None:
        current_state = await state.get_state()
        if current_state is not None:
            # В FSM — спец-хендлеры (note, pick, swipe) сработают раньше
            return
        text = (message.text or "").strip()
        if not text or text in _BUTTON_TEXTS:
            return
        if len(text) < 2:
            return
        await _do_search_and_show(message.bot, message.chat.id, message.from_user.id, text, state=state)


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
                )
            )
        await query.answer(results, cache_time=60, is_personal=True)

    return router
