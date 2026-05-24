"""Парсинг публичных Telegram каналов через t.me/s/<channel>.

Без Telethon, без логина — публичная HTML-страница с превью постов.
Используется как fallback-источник трейлеров.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Каналы по которым ищем трейлеры
TRAILER_CHANNELS = ["kinotreilery", "movieshakers", "trailer_video"]


async def search_trailer_in_channels(query: str, *, limit: int = 5) -> list[str]:
    """Возвращает список URL постов с трейлерами, где встречается query.

    URL вида https://t.me/<channel>/<post_id> — Telegram сам отрисует превью с видео.
    """
    query_norm = query.lower().strip()
    results: list[str] = []
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for ch in TRAILER_CHANNELS:
            try:
                resp = await client.get(f"https://t.me/s/{ch}")
                if resp.status_code != 200:
                    continue
                html = resp.text
            except Exception as e:
                logger.debug("Channel %s fetch failed: %s", ch, e)
                continue
            # Поиск блоков с постами и проверка совпадения
            posts = re.findall(
                r'data-post="([^"]+)".*?class="tgme_widget_message_text[^"]*"[^>]*>(.+?)</div>',
                html,
                flags=re.DOTALL,
            )
            for post_id, body_html in posts:
                # Уберём HTML
                text = re.sub(r"<[^>]+>", " ", body_html).lower()
                if query_norm in text:
                    results.append(f"https://t.me/{post_id}")
                    if len(results) >= limit:
                        return results
    return results
