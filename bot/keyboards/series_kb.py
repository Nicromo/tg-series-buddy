"""Inline keyboards for series cards."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def card_keyboard(
    series_id: int,
    *,
    has_trailer: bool,
    is_watched: bool = False,
) -> InlineKeyboardMarkup:
    """Buttons under series card.

    For 'watched' series we add a single 'Хочу пересмотреть' button
    instead of the regular status row (the series is already finished).
    """
    rows: list[list[InlineKeyboardButton]] = []

    if is_watched:
        rows.append(
            [InlineKeyboardButton(text="🔁 Хочу пересмотреть", callback_data=f"st:want_rewatch:{series_id}")]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(text="👀 Хочу", callback_data=f"st:want:{series_id}"),
                InlineKeyboardButton(text="▶️ Смотрю", callback_data=f"st:watching:{series_id}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="✅ Посмотрел", callback_data=f"st:watched:{series_id}"),
                InlineKeyboardButton(text="❌ Дропнул", callback_data=f"st:dropped:{series_id}"),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text="👍 Лайк", callback_data=f"rt:like:{series_id}"),
            InlineKeyboardButton(text="👎 Дизлайк", callback_data=f"rt:dislike:{series_id}"),
        ]
    )

    if has_trailer:
        rows.append(
            [InlineKeyboardButton(text="🎥 Показать трейлер", callback_data=f"tr:{series_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results_keyboard(hits: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"pick:{kp_id}")]
        for kp_id, label in hits
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checkin_keyboard(series_id: int) -> InlineKeyboardMarkup:
    """Weekly check-in keyboard: 'finished / still watching / dropped'."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Досмотрел", callback_data=f"ck:fin:{series_id}"),
                InlineKeyboardButton(text="▶️ Ещё смотрю", callback_data=f"ck:cont:{series_id}"),
            ],
            [InlineKeyboardButton(text="❌ Дропнул", callback_data=f"ck:drop:{series_id}")],
        ]
    )


def rating_only_keyboard(series_id: int) -> InlineKeyboardMarkup:
    """After 'finished' check-in -- ask for rating if not yet set."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Лайк", callback_data=f"rt:like:{series_id}"),
                InlineKeyboardButton(text="👎 Дизлайк", callback_data=f"rt:dislike:{series_id}"),
            ]
        ]
    )
