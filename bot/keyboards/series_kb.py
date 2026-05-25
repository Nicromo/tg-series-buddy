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

    # Кнопка трейлера ВСЕГДА — если YouTube-id нет, фолбэк на TG-канал
    rows.append(
        [InlineKeyboardButton(text="🎥 Показать трейлер", callback_data=f"tr:{series_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results_keyboard(hits: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Кнопки выбора варианта поиска: '1️⃣ Название (2020)' + 'Ничего не подходит'."""
    digits = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    rows = []
    for i, (kp_id, label) in enumerate(hits[:10]):
        prefix = digits[i] if i < len(digits) else f"{i+1}."
        rows.append([InlineKeyboardButton(text=f"{prefix} {label}", callback_data=f"pick:{kp_id}")])
    rows.append([InlineKeyboardButton(text="❌ Ничего из этого", callback_data="pick:none")])
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

def trailer_link_keyboard(url: str) -> InlineKeyboardMarkup:
    """Одна кнопка-ссылка для трейлера — открывает YouTube в приложении."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="▶️ Открыть в YouTube", url=url)]]
    )


def bulk_move_keyboard(from_status: str, to_status: str, count: int) -> InlineKeyboardMarkup:
    """Кнопка 'перевести все' под списком."""
    labels = {
        ("want", "watching"): f"▶️ Начать смотреть все ({count})",
        ("watching", "watched"): f"✅ Отметить все досмотренными ({count})",
    }
    label = labels.get((from_status, to_status), f"Перевести все ({count})")
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"bulk:{from_status}:{to_status}")]]
    )
