"""Поиск трейлеров.

Стратегия: YouTube URL отдаём текстом — Telegram сам отрисует preview
с тумбнейлом и кнопкой Play. yt-dlp не используется: на Render YouTube
блокирует серверные IP (`Sign in to confirm you're not a bot`).

Здесь остался только фолбэк: поиск трейлера в публичных TG-каналах,
когда у KP вообще нет YouTube-ссылки.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def find_trailer_tg_link(title_ru: str, year=None) -> Optional[str]:
    """Ищет ссылку на пост с трейлером в публичных TG-каналах (t.me/s/...).

    Возвращает URL вида https://t.me/<channel>/<post_id> — Telegram сам
    отрисует превью с видео.
    """
    try:
        from .tg_channel_parser import search_trailer_in_channels
        q = title_ru if not year else f"{title_ru} {year}"
        links = await search_trailer_in_channels(q, limit=1)
        if links:
            return links[0]
    except Exception as e:
        logger.warning("TG trailer search failed: %s", e)
    return None
