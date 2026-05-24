"""Инлайн-клавиатуры для карточек сериалов."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def card_keyboard(
    series_id: int,
    *,
    has_trailer: bool,
    is_watched: bool = False,
) -> InlineKeyboardMarkup:
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
                InlineKeyboardButton(text="✅ Досмотрел", callback_data=f"st:watched:{series_id}"),
                InlineKeyboardButton(text="❌ Дропнул", callback_data=f"st:dropped:{series_id}"),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text="👍", callback_data=f"rt:like:{series_id}"),
            InlineKeyboardButton(text="👎", callback_data=f"rt:dislike:{series_id}"),
            InlineKeyboardButton(text="📝", callback_data=f"note:{series_id}"),
            InlineKeyboardButton(text="📤", callback_data=f"share:{series_id}"),
        ]
    )

    if has_trailer:
        rows.append(
            [InlineKeyboardButton(text="🎥 Показать трейлер", callback_data=f"tr:{series_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results_keyboard(hits: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Кнопки выбора варианта поиска: '1️⃣ Название (2020)'."""
    digits = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    rows = []
    for i, (kp_id, label) in enumerate(hits[:10]):
        prefix = digits[i] if i < len(digits) else f"{i+1}."
        rows.append([InlineKeyboardButton(text=f"{prefix} {label}", callback_data=f"pick:{kp_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checkin_keyboard(series_id: int) -> InlineKeyboardMarkup:
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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Лайк", callback_data=f"rt:like:{series_id}"),
                InlineKeyboardButton(text="👎 Дизлайк", callback_data=f"rt:dislike:{series_id}"),
            ]
        ]
    )


def swipe_keyboard(series_id: int, queue_idx: int) -> InlineKeyboardMarkup:
    """Кнопки для свайп-вечера: лайк / пропуск / стоп."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👎 Скип", callback_data=f"sw:no:{series_id}:{queue_idx}"),
                InlineKeyboardButton(text="❤️ Хочу!", callback_data=f"sw:yes:{series_id}:{queue_idx}"),
            ],
            [InlineKeyboardButton(text="🛑 Хватит на сегодня", callback_data=f"sw:stop:0:{queue_idx}")],
        ]
    )
