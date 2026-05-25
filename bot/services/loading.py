"""Анимация-плейсхолдер пока бот думает.

start_loading() шлёт анимированное сообщение, возвращает message_id.
stop_loading(message_id) удаляет его.

Источники анимации в порядке приоритета:
1. env LOADING_GIF_URLS — список URL через запятую (Tenor/Giphy/CDN).
2. send_dice — нативная Telegram-анимация с эмодзи 🎲/🎯/🎰/⚽/🏀
   (играет ~3 сек). Не требует URL, всегда работает, выглядит живо.
3. Fallback — текстовое сообщение с эмодзи.

Поведение по умолчанию: если LOADING_GIF_URLS не задан, используем
send_dice (живая анимация). Если хочешь отключить и оставить только
текст: LOADING_DICE=false в env.
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


_PHRASES = [
    "🍿 Думаю что предложить…",
    "🎬 Подбираю что-то стоящее…",
    "🛋 Раздвигаю диван и копаюсь в идеях…",
    "📺 Включаю интуицию…",
    "🪄 Колдую над списком…",
    "🎥 Перебираю варианты…",
    "🔮 Заглядываю в кинохрустальный шар…",
]


_DICE_EMOJIS = ["🎲", "🎯", "🎰", "⚽", "🏀", "🎳"]


def _dice_enabled() -> bool:
    return os.getenv("LOADING_DICE", "true").lower() in ("1", "true", "yes")


async def start_loading(bot: Bot, chat_id: int) -> Optional[int]:
    """Показывает «думаю» сообщение. Возвращает message_id для удаления."""
    urls = _load_urls()
    if urls:
        gif = random.choice(urls)
        try:
            m = await bot.send_animation(chat_id, animation=gif)
            return m.message_id
        except Exception as e:
            logger.warning("loading gif %s failed: %s", gif, e)
    # Нативная Telegram-анимация: send_dice играет ~3 сек.
    if _dice_enabled():
        try:
            m = await bot.send_dice(chat_id, emoji=random.choice(_DICE_EMOJIS))
            return m.message_id
        except Exception as e:
            logger.warning("loading dice failed: %s", e)
    # Fallback: текст с фразой
    try:
        m = await bot.send_message(chat_id, random.choice(_PHRASES))
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
        async with with_loader(bot, chat_id):
            ...твоя долгая работа...
    Сам создаёт и удаляет loading-сообщение."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.msg_id: Optional[int] = None

    async def __aenter__(self):
        self.msg_id = await start_loading(self.bot, self.chat_id)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await stop_loading(self.bot, self.chat_id, self.msg_id)
        return False
