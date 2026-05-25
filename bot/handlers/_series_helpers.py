"""Общие хелперы для handlers/series.py.

Вынесено отдельно чтобы:
- series.py не разрастался > 1100 строк
- эти функции были тестируемы независимо от aiogram-роутера
- легче было дальше дробить series.py по командам
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from aiogram import Bot
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import repository as repo
from ..db.models import Series
from ..keyboards.series_kb import card_keyboard
from ..services.kinopoisk import KPDetails, KinopoiskClient

logger = logging.getLogger(__name__)


STATUS_LABELS = {
    "want": "👀 Хочу посмотреть",
    "watching": "▶️ Смотрю",
    "watched": "✅ Досмотрел",
    "want_rewatch": "🔁 Хочу пересмотреть",
    "dropped": "❌ Дропнул",
}
RATING_LABELS = {"like": "👍 Лайк", "dislike": "👎 Дизлайк"}

DIGIT_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


class NoteFSM(StatesGroup):
    waiting = State()


class SwipeFSM(StatesGroup):
    swiping = State()


class PickFSM(StatesGroup):
    """Ожидание выбора варианта (цифрой или кнопкой) после /add или фото."""
    choosing = State()


def format_caption(
    s: Series,
    *,
    status: Optional[str] = None,
    rating: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """Подпись под карточкой сериала. Чистая функция — для тестов."""
    lines: list[str] = []
    title = f"🎬 <b>{s.title_ru}</b>"
    if s.title_en and s.title_en != s.title_ru:
        title += f" / <i>{s.title_en}</i>"
    if s.year:
        title += f" ({s.year})"
    lines.append(title)
    if s.kp_id:
        lines.append(f'<a href="https://www.kinopoisk.ru/film/{s.kp_id}/">🔗 Открыть на Кинопоиске</a>')

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
        if len(desc) > 500:
            desc = desc[:500].rstrip() + "…"
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

    if getattr(s, "watch_options_json", None):
        try:
            opts = json.loads(s.watch_options_json)
        except Exception:
            opts = []
        if opts:
            wl = ", ".join(f'<a href="{u}">{n}</a>' for n, u in opts[:5])
            lines.append("")
            lines.append(f"📺 Смотреть: {wl}")

    if note:
        lines.append(f"\n📝 <i>{note}</i>")

    return "\n".join(lines)


async def send_card(
    bot: Bot,
    chat_id: int,
    series: Series,
    *,
    user_status: Optional[str] = None,
    user_rating: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    caption = format_caption(series, status=user_status, rating=user_rating, note=note)
    kb = card_keyboard(
        series.id,
        has_trailer=bool(series.trailer_youtube_id or series.trailer_file_id),
        is_watched=user_status == "watched",
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
    await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=kb)


def details_to_series_dict(d: KPDetails) -> dict:
    """KPDetails → dict для upsert_series_from_dict. Чистая — для тестов."""
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
        "trailer_language": d.best_trailer_language,
        "watch_options_json": json.dumps(d.watch_options) if d.watch_options else None,
    }


async def add_by_kp_id(
    bot: Bot,
    chat_id: int,
    tg_user_id: int,
    kp_id: int,
    session_factory: async_sessionmaker,
    kp: KinopoiskClient,
) -> None:
    """Достать детали из KP, сохранить в БД, привязать к юзеру (want),
    зеркальнуть партнёру в паре. В конце — карточка."""
    try:
        details = await kp.get_details(kp_id)
    except Exception as e:
        logger.exception("KP details failed")
        await bot.send_message(chat_id, f"😕 Не получилось загрузить детали: {e}")
        return
    mirrored_to_partner = False
    async with session_factory() as session:
        user = await repo.get_or_create_user(session, tg_id=tg_user_id, username=None, full_name=None)
        await repo.upsert_series_from_dict(session, details_to_series_dict(details))
        await session.commit()
        series = await repo.get_series_by_kp_id(session, details.kp_id)
        us = await repo.get_user_series(session, tg_user_id, series.id)
        was_existing = us is not None
        if us is None:
            us = await repo.set_user_series_status(session, tg_user_id, series.id, "want")
            await session.commit()
        if user.pair_id:
            members = await repo.get_pair_members(session, user.pair_id)
            for member in members:
                if member.id == tg_user_id:
                    continue
                partner_us = await repo.get_user_series(session, member.id, series.id)
                if partner_us is None:
                    await repo.set_user_series_status(session, member.id, series.id, "want")
                    mirrored_to_partner = True
            await session.commit()
        status = us.status if us else None
        rating = us.rating if us else None
        note = us.notes if us else None

    if was_existing:
        # Контекстная подсказка: какую кнопку жать в зависимости от текущего статуса
        hints = {
            "want": "Жми ▶️ если уже начал смотреть.",
            "watching": "Жми ✅ если досмотрел.",
            "watched": "Жми 🔁 если хочешь пересмотреть.",
            "want_rewatch": "Жми ▶️ когда возьмёшься за пересмотр.",
            "dropped": "Жми 👀 чтобы вернуть в очередь.",
        }
        hint = hints.get(status, "Меняй статус кнопками ниже 👇")
        await bot.send_message(
            chat_id,
            f"💡 <b>{series.title_ru}</b> уже у тебя — статус: "
            f"{STATUS_LABELS.get(status, status)}.\n{hint}",
            parse_mode="HTML",
        )
    else:
        suffix = " 👫 (общий список с партнёром)" if mirrored_to_partner else ""
        await bot.send_message(
            chat_id,
            f"✅ <b>{series.title_ru}</b> добавлен в «👀 Хочу посмотреть»{suffix}",
            parse_mode="HTML",
        )
    await send_card(bot, chat_id, series, user_status=status, user_rating=rating, note=note)
