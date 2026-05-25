"""Анимация-плейсхолдер пока бот думает.

start_loading() шлёт анимированное сообщение, возвращает message_id.
stop_loading(message_id) удаляет его.

Источники GIF в порядке приоритета:
1. env LOADING_GIF_URLS — список URL через запятую (Tenor/Giphy/CDN).
2. Fallback — обычное сообщение с эмодзи (Telegram сам анимирует
   одиночные эмодзи, выглядит почти как стикер).

Пример env (можно положить любые забавные гифки):
  LOADING_GIF_URLS=https://media.tenor.com/abc.gif,https://media.tenor.com/xyz.gif
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


async def start_loading(bot: Bot, chat_id: int) -> Optional[int]:
    """Показывает «думаю» сообщение. Возвращает message_id для удаления.
    None если ничего не отправлено (редкий случай ошибки)."""
    urls = _load_urls()
    if urls:
        gif = random.choice(urls)
        try:
            m = await bot.send_animation(chat_id, animation=gif)
            return m.message_id
        except Exception as e:
            logger.warning("loading gif %s failed: %s", gif, e)
    # Fallback: одиночное сообщение с эмодзи — Telegram анимирует
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
