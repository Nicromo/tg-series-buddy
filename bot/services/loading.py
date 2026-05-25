"""Анимация-плейсхолдер пока бот думает.

start_loading(bot, chat_id, context="...") → message_id для удаления.
stop_loading(bot, chat_id, message_id) — удаляет.

Контекст ('suggest', 'trailer', 'search', 'cinema', 'seasons',
'subscribe', 'wallpaper', None) подбирает **тематический эмодзи**
и фразу. Используем обычное `send_message` — Telegram анимирует
одиночный большой эмодзи + удаляется мгновенно.

ПОЧЕМУ НЕ send_dice: dice/боулинг/кубик не относятся к контексту
бота, плюс Telegram не даёт удалять dice пока он играет (3 сек) —
анимация «висит» на экране.

GIF через env LOADING_GIF_URLS — приоритет если задан.
"""

from __future__ import annotations

import logging
import os
import random
from typing import Optional

from aiogram import Bot

logger = logging.getLogger(__name__)


def _load_urls() -> list[str]:
    raw = os.getenv("LOADING_GIF_URLS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


# Тематические наборы фраз — каждой длинной операции свой эмодзи и текст
_PHRASES_BY_CONTEXT: dict[str, list[str]] = {
    "suggest": [
        "🎬 Думаю что предложить…",
        "🍿 Подбираю что-то стоящее…",
        "🪄 Колдую над подборкой…",
        "🛋 Раздвигаю диван и роюсь в идеях…",
        "🔮 Заглядываю в кинохрустальный шар…",
    ],
    "trailer": [
        "🎥 Ищу трейлер…",
        "🎞 Перебираю источники…",
        "🍿 Разворачиваю плёнку…",
    ],
    "search": [
        "🔎 Ищу в базе…",
        "📚 Перебираю архив Кинопоиска…",
    ],
    "cinema": [
        "🎫 Заглядываю в кассу…",
        "📅 Смотрю расписание сеансов…",
    ],
    "seasons": [
        "📺 Считаю эпизоды…",
        "🎞 Открываю расписание сезонов…",
    ],
    "subscribe": [
        "📡 Подключаюсь к каналу…",
        "🔔 Настраиваю уведомления…",
    ],
    "wallpaper": [
        "🖼 Собираю постер…",
        "🎨 Раскладываю обложки…",
    ],
    "default": [
        "🎬 Думаю…",
        "🍿 Сейчас…",
        "🪄 Минутку…",
    ],
}


async def start_loading(bot: Bot, chat_id: int, *, context: str = "default") -> Optional[int]:
    """Показывает «думаю» сообщение с тематическим эмодзи."""
    urls = _load_urls()
    if urls:
        gif = random.choice(urls)
        try:
            m = await bot.send_animation(chat_id, animation=gif)
            return m.message_id
        except Exception as e:
            logger.warning("loading gif %s failed: %s", gif, e)
    # Обычное сообщение — мгновенно удаляется (в отличие от dice)
    phrases = _PHRASES_BY_CONTEXT.get(context) or _PHRASES_BY_CONTEXT["default"]
    try:
        m = await bot.send_message(chat_id, random.choice(phrases))
        return m.message_id
    except Exception as e:
        logger.warning("loading message failed: %s", e)
        return None


async def stop_loading(bot: Bot, chat_id: int, message_id: Optional[int]) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        # Не critical — например юзер сам удалил
        pass


class with_loader:
    """Async context manager:
        async with with_loader(bot, chat_id, context="trailer"):
            ...долгая работа...
    """

    def __init__(self, bot: Bot, chat_id: int, *, context: str = "default"):
        self.bot = bot
        self.chat_id = chat_id
        self.context = context
        self.msg_id: Optional[int] = None

    async def __aenter__(self):
        self.msg_id = await start_loading(self.bot, self.chat_id, context=self.context)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await stop_loading(self.bot, self.chat_id, self.msg_id)
        return False
