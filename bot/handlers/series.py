"""Handlers: /add, /list, /watching, /watched, /rewatch, /random, /match, /checkin
and all callback buttons. Data source: kinopoisk.dev.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    Message,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from ..db import repository as repo
from ..db.models import Series
from ..keyboards.series_kb import (
    card_keyboard,
    checkin_keyboard,
    rating_only_keyboard,
    search_results_keyboard,
)
from ..services.kinopoisk import KinopoiskClient, KPDetails
from ..services.scheduler import run_weekly_checkin
from ..services.trailer import fetch_best_trailer

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "want": "👀 Хочу посмотреть",
    "watching": "▶️ Смотрю",
    "watched": "✅ Посмотрел",
    "want_rewatch": "🔁 Хочу пересмотреть",
    "dropped": "❌ Дропнул",
}

RATING_LABELS = {"like": "👍 Лайк", "dislike": "👎 Дизлайк"}


def _format_caption(s: Series, *, status: Optional[str] = None, rating: Optional[str] = None) -> str:
    lines: list[str] = []
    title = f"🎬 <b>{s.title_ru}</b>"
    if s.title_en and s.title_en != s.title_ru:
        title += f" / <i>{s.title_en}</i>"
    if s.year:
        title += f" ({s.year})"
    lines.append(title)

    rating_bits = []
    if s.rating_kp:
        rating_bits.append(f"⭐ КП {s.rating_kp:.1f}")
    if s.rating_imdb:
        rating_bits.append(f"IMDb {s.rating_imdb:.1f}")
    if rating_bits:
        lines.append(" · ".join(rating_bits))

    meta_bits = []
    if s.seasons:
        meta_bits.append(f"📺 {s.seasons} сез.")
    if s.status_kp:
        meta_bits.append(s.status_kp)
    if meta_bits:
        lines.append(" • ".join(meta_bits))

    if s.genres:
        lines.append(f"🎭 {s.genres}")

    if s.description_ru:
        desc = s.description_ru
        if len(desc) > 600:
            desc = desc[:600].rstrip() + "…"
        lines.append("")
        lines.append(desc)

    pinned = []
    if status:
        pinned.append(STATUS_LABELS.get(status, status))
    if rating:
        pinned.append(RATING_LABELS.get(rating, rating))
    if pinned:
        lines.append("")
        lines.append("• " + " • ".join(pinned))

    return "\n".join(lines)


async def _send_card(
    bot: Bot,
    chat_id: int,
    series: Series,
    *,
    user_status: Optional[str] = None,
    user_rating: Optional[str] = None,
) -> None:
    caption = _format_caption(series, status=user_status, rating=user_rating)
    is_watched = user_status == "watched"
    kb = card_keyboard(
        series.id,
        has_trailer=bool(series.trailer_youtube_id or series.trailer_file_id),
        is_watched=is_watched,
    )

    if series.poster_url:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=series.poster_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
        except Exception as e:
            logger.warning("send_photo failed: %s — falling back to text", e)

    await bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode="HTML",
        reply_markup=kb,
    )


def _details_to_series_dict(d: KPDetails) -> dict:
    return {
        "kp_id": d.kp_id,
        "title_ru": d.title_ru,
        "title_en": d.title_en,
        "year": d.year,
        "poster_url": d.poster_url,
        "description_ru": d.description_ru,
        "genres": ", ".join(d.genres) if d.genres else None,
        "rating_kp": d.rating_kp,
        "rating_imdb": d.rating_imdb,
        "seasons": d.seasons,
        "status_kp": d.status_kp,
        "trailer_youtube_id": d.best_trailer_youtube_id,
        "trailer_language": "ru" if d.trailers else None,
    }


def make_router(
    session_factory: async_sessionmaker,
    kp: KinopoiskClient,
    settings: Settings,
) -> Router:
    router = Router(name="series")

    @router.message(Command("add"))
    async def cmd_add(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Напиши: <code>/add Severance</code>", parse_mode="HTML")
            return
        query = parts[1].strip()

        await message.bot.send_chat_action(message.chat.id, action="typing")
        try:
            hits = await kp.search(query, limit=5)
        except Exception as e:
            logger.exception("KP search failed")
            await message.answer(f"😕 Не получилось найти: {e}")
            return

        if not hits:
            await message.answer("Ничего не нашёл (или это не сериал). Попробуй другое название.")
            return

        if len(hits) == 1:
            await _add_by_kp_id(
                message.bot, message.chat.id, message.from_user.id, hits[0].kp_id, session_factory, kp
            )
            return

        kb_items = []
        for h in hits:
            label = h.title_ru + (f" ({h.year})" if h.year else "")
            kb_items.append((h.kp_id, label))
        await message.answer(
            "Нашёл несколько вариантов — выбери:",
            reply_markup=search_results_keyboard(kb_items),
        )

    @router.callback_query(F.data.startswith("pick:"))
    async def cb_pick(call: CallbackQuery) -> None:
        kp_id = int(call.data.split(":")[1])
        await call.answer("Загружаю детали…")
        await _add_by_kp_id(
            call.bot, call.message.chat.id, call.from_user.id, kp_id, session_factory, kp
        )
        try:
            await call.message.delete()
        except Exception:
            pass

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
            await repo.set_user_series_status(session, call.from_user.id, series_id, status)
            await session.commit()
        await call.answer(f"Ок: {STATUS_LABELS.get(status, status)}")

    @router.callback_query(F.data.startswith("rt:"))
    async def cb_rating(call: CallbackQuery) -> None:
        _, rating, series_id_s = call.data.split(":")
        series_id = int(series_id_s)
        async with session_factory() as session:
            await repo.get_or_create_user(
                session,
                tg_id=call.from_user.id,
                username=call.from_user.username,
                full_name=call.from_user.full_name,
            )
            await repo.set_user_series_rating(session, call.from_user.id, series_id, rating)
            await session.commit()
        await call.answer(f"Ок: {RATING_LABELS.get(rating, rating)}")

    @router.callback_query(F.data.startswith("ck:"))
    async def cb_checkin(call: CallbackQuery) -> None:
        """Weekly check-in: 'finished / still watching / dropped'."""
        _, action, series_id_s = call.data.split(":")
        series_id = int(series_id_s)

        if action == "fin":
            # Mark as watched
            async with session_factory() as session:
                await repo.get_or_create_user(
                    session,
                    tg_id=call.from_user.id,
                    username=call.from_user.username,
                    full_name=call.from_user.full_name,
                )
                us = await repo.set_user_series_status(session, call.from_user.id, series_id, "watched")
                await session.commit()
                # If no rating yet -- ask for it
                if not us.rating:
                    series = await session.get(Series, series_id)
                    title = series.title_ru if series else "сериал"
                    await call.message.answer(
                        f"Поставь оценку <b>{title}</b>:",
                        parse_mode="HTML",
                        reply_markup=rating_only_keyboard(series_id),
                    )
            await call.answer("Ок: ✅ Досмотрел")
        elif action == "cont":
            # Still watching -- just record the check-in
            async with session_factory() as session:
                await repo.mark_checkin_sent(session, call.from_user.id, series_id)
                await session.commit()
            await call.answer("Ок: ▶️ Спрошу через неделю")
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
            await call.answer("Ок: ❌ Дропнул")

    @router.callback_query(F.data.startswith("tr:"))
    async def cb_trailer(call: CallbackQuery) -> None:
        series_id = int(call.data.split(":")[1])
        await call.answer("Готовлю трейлер… пара секунд")

        async with session_factory() as session:
            series = await session.get(Series, series_id)
            if series is None:
                await call.message.answer("Не нашёл сериал в БД.")
                return

            if series.trailer_file_id:
                try:
                    await call.bot.send_video(
                        chat_id=call.message.chat.id,
                        video=series.trailer_file_id,
                        caption=f"🎥 Трейлер · {series.title_ru}",
                    )
                    return
                except Exception as e:
                    logger.warning("Cached file_id failed, will re-download: %s", e)
                    series.trailer_file_id = None
                    await session.flush()

            yt_id = series.trailer_youtube_id
            title = series.title_ru
            year = series.year
            tmdb_lang = series.trailer_language

        await call.bot.send_chat_action(call.message.chat.id, action="upload_video")
        path, source = await fetch_best_trailer(
            title_ru=title,
            year=year,
            youtube_id=yt_id,
            tmdb_language=tmdb_lang,
            out_dir=settings.trailer_tmp_dir,
            max_mb=settings.max_trailer_mb,
        )

        if path is None:
            await call.message.answer("😕 Не получилось скачать трейлер.")
            return

        try:
            msg = await call.bot.send_video(
                chat_id=call.message.chat.id,
                video=FSInputFile(path),
                caption=f"🎥 Трейлер · {title} · источник: {source}",
                supports_streaming=True,
            )
            if msg.video and msg.video.file_id:
                async with session_factory() as session:
                    series = await session.get(Series, series_id)
                    if series:
                        series.trailer_file_id = msg.video.file_id
                        await session.commit()
        except Exception as e:
            logger.exception("Failed to send video")
            await call.message.answer(f"😕 Telegram отказался: {e}")
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    async def _send_list(message: Message, status: str, empty_msg: str) -> None:
        async with session_factory() as session:
            rows = await repo.list_user_series(session, message.from_user.id, status=status)
        if not rows:
            await message.answer(empty_msg)
            return
        for us, series in rows[:20]:
            await _send_card(
                message.bot,
                message.chat.id,
                series,
                user_status=us.status,
                user_rating=us.rating,
            )

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        await _send_list(message, "want", "Очередь пустая. Добавь /add &lt;название&gt;")

    @router.message(Command("watching"))
    async def cmd_watching(message: Message) -> None:
        await _send_list(message, "watching", "Сейчас ничего не смотришь.")

    @router.message(Command("watched"))
    async def cmd_watched(message: Message) -> None:
        await _send_list(message, "watched", "Ещё ничего не досмотрел до конца.")

    @router.message(Command("rewatch"))
    async def cmd_rewatch(message: Message) -> None:
        await _send_list(message, "want_rewatch", "Список пересмотра пустой. Жми 🔁 в карточке досмотренного сериала.")

    @router.message(Command("random"))
    async def cmd_random(message: Message) -> None:
        async with session_factory() as session:
            rows = await repo.list_user_series(session, message.from_user.id, status="want")
        if not rows:
            await message.answer("Очередь пустая.")
            return
        us, series = random.choice(rows)
        await _send_card(message.bot, message.chat.id, series, user_status=us.status, user_rating=us.rating)

    @router.message(Command("match"))
    async def cmd_match(message: Message) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session,
                tg_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )
            if not user.pair_id:
                await message.answer("Сначала свяжись с партнёром через /pair — иначе не с кем мэтчиться 🙂")
                return
            matches = await repo.list_pair_matches(session, user.pair_id)
        if not matches:
            await message.answer("Пока нет общих лайков. Лайкайте сериалы — будет 💛")
            return
        await message.answer(f"💛 Лайкнули оба ({len(matches)}):")
        for series in matches[:20]:
            await _send_card(message.bot, message.chat.id, series)

    @router.message(Command("checkin"))
    async def cmd_checkin_manual(message: Message) -> None:
        """Manually trigger the weekly check-in (for testing / on-demand)."""
        sent = await run_weekly_checkin(message.bot, session_factory)
        await message.answer(f"🔔 Опрос отправлен: {sent} сериал(ов).")

    return router


async def _add_by_kp_id(
    bot: Bot,
    chat_id: int,
    tg_user_id: int,
    kp_id: int,
    session_factory: async_sessionmaker,
    kp: KinopoiskClient,
) -> None:
    try:
        details = await kp.get_details(kp_id)
    except Exception as e:
        logger.exception("KP details failed")
        await bot.send_message(chat_id, f"😕 Не получилось загрузить детали: {e}")
        return

    async with session_factory() as session:
        await repo.get_or_create_user(session, tg_id=tg_user_id, username=None, full_name=None)
        await repo.upsert_series_from_dict(session, _details_to_series_dict(details))
        await session.commit()
        series = await repo.get_series_by_kp_id(session, details.kp_id)

    await _send_card(bot, chat_id, series)
