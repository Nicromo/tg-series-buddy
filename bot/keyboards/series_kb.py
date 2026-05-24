"""Inline-клавиатуры для карточек сериалов."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def card_keyboard(series_id: int, *, has_trailer: bool) -> InlineKeyboardMarkup:
    """Клавиатура под карточкой сериала: статусы + лайки + трейлер."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="👀 Хочу", callback_data=f"st:want:{series_id}"),
            InlineKeyboardButton(text="▶️ Смотрю", callback_data=f"st:watching:{series_id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Посмотрел", callback_data=f"st:watched:{series_id}"),
            InlineKeyboardButton(text="❌ Дропнул", callback_data=f"st:dropped:{series_id}"),
        ],
        [
            InlineKeyboardButton(text="👍 Лайк", callback_data=f"rt:like:{series_id}"),
            InlineKeyboardButton(text="👎 Дизлайк", callback_data=f"rt:dislike:{series_id}"),
        ],
    ]
    if has_trailer:
        rows.append(
            [InlineKeyboardButton(text="🎥 Показать трейлер", callback_data=f"tr:{series_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results_keyboard(hits: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Список вариантов поиска: '<название> (год)' → callback с tmdb_id."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"pick:{tmdb_id}")]
        for tmdb_id, label in hits
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
