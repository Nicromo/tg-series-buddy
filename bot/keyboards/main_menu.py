"""Главное меню снизу — ReplyKeyboard."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu() -> ReplyKeyboardMarkup:
    """Главное меню (всегда видно снизу чата)."""
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Что включаем сегодня? 🍿",
        keyboard=[
            # Компактные 4 ряда — Профиль/Пара/Помощь/Поддержать доступны через /menu
            [KeyboardButton(text="🎬 Добавить"), KeyboardButton(text="🎲 Что включить?")],
            [KeyboardButton(text="👀 Хочу"), KeyboardButton(text="▶️ Смотрю"), KeyboardButton(text="✅ Посмотрел")],
            [KeyboardButton(text="✨ Подобрать"), KeyboardButton(text="🔥 Новинки"), KeyboardButton(text="🔁 Пересмотреть")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="📂 Меню"), KeyboardButton(text="ℹ️ Помощь")],
        ],
    )
