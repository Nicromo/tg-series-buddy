"""YouTube подписки через RSS (без API ключа).

RSS feed по channel_id отдаёт XML с 15 последними видео:
    https://www.youtube.com/feeds/videos.xml?channel_id=UCxxx

@handle страница резолвится в channel_id через парсинг HTML
(нужен CONSENT cookie чтобы пропустить consent gate ЕС).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Cookie чтобы YouTube не редиректил на consent.youtube.com
_CONSENT_COOKIE = "CONSENT=YES+1; SOCS=CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass
class ChannelInfo:
    channel_id: str  # UCxxx (24 символа, начинаются с UC)
    title: str


@dataclass
class VideoEntry:
    video_id: str
    title: str
    published: str  # ISO datetime
    url: str  # https://www.youtube.com/watch?v=...


# ---------- Резолв URL → channel_id ----------

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def _is_channel_id(s: str) -> bool:
    return bool(_CHANNEL_ID_RE.match(s))


async def resolve_channel(url_or_handle: str) -> Optional[ChannelInfo]:
    """Принимает любую форму ввода YouTube канала:
    - URL /channel/UCxxx — прямой channel_id
    - URL /@handle — резолвится через HTML (с consent cookie)
    - просто @handle или channel handle text — обернётся
    - просто UCxxx — берётся как channel_id
    Возвращает (channel_id, title) или None.
    """
    raw = (url_or_handle or "").strip()
    if not raw:
        return None

    # Уже channel_id
    if _is_channel_id(raw):
        title = await _fetch_channel_title(raw)
        return ChannelInfo(channel_id=raw, title=title or raw)

    # /channel/UCxxx в URL
    m = re.search(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})", raw)
    if m:
        ch_id = m.group(1)
        title = await _fetch_channel_title(ch_id)
        return ChannelInfo(channel_id=ch_id, title=title or ch_id)

    # /@handle
    if raw.startswith("@") or "youtube.com/@" in raw:
        handle = raw
        if "youtube.com/@" in raw:
            m = re.search(r"youtube\.com/(@[A-Za-z0-9._-]+)", raw)
            if m:
                handle = m.group(1)
        elif not handle.startswith("@"):
            handle = "@" + handle.lstrip("@")
        return await _resolve_handle(handle)

    # /c/CustomName, /user/Username — попробуем как handle (часто работает)
    m = re.search(r"youtube\.com/(?:c|user)/([A-Za-z0-9._-]+)", raw)
    if m:
        return await _resolve_handle("@" + m.group(1))

    # Голый текст — попробуем как handle
    if re.match(r"^[A-Za-z0-9._-]+$", raw):
        return await _resolve_handle("@" + raw)

    return None


async def _resolve_handle(handle: str) -> Optional[ChannelInfo]:
    """@handle → channel_id через парсинг HTML страницы канала.
    Title берём из RSS feed (там UTF-8 в чистом виде, без HTML/JSON escape).
    """
    url = f"https://www.youtube.com/{handle}"
    headers = {"User-Agent": _UA, "Cookie": _CONSENT_COOKIE, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code != 200:
                logger.info("YT handle resolve %s: status %s", handle, r.status_code)
                return None
            html = r.text
            patterns = [
                r'"channelId":"(UC[A-Za-z0-9_-]{22})"',
                r'"externalId":"(UC[A-Za-z0-9_-]{22})"',
                r'/channel/(UC[A-Za-z0-9_-]{22})',
            ]
            ch_id: Optional[str] = None
            for p in patterns:
                m = re.search(p, html)
                if m:
                    ch_id = m.group(1)
                    break
            if not ch_id:
                return None
            # Title — самый надёжно из RSS feed (там корректный UTF-8).
            # На HTML-странице YouTube строки JSON могут содержать \uXXXX,
            # которые надо парсить через json.loads — НЕ через unicode_escape
            # (ломает кириллические байты).
            title = await _fetch_channel_title(ch_id)
            if not title:
                m = re.search(r'<meta name="title" content="([^"]+)"', html)
                if m:
                    title = m.group(1)
            if not title:
                m = re.search(r'"channelMetadataRenderer":\{"title":"((?:[^"\\]|\\.)+)"', html)
                if m:
                    try:
                        import json as _json
                        title = _json.loads(f'"{m.group(1)}"')
                    except Exception:
                        title = m.group(1)
            return ChannelInfo(channel_id=ch_id, title=title or handle)
    except Exception as e:
        logger.warning("YT handle resolve %s failed: %s", handle, e)
        return None


async def _fetch_channel_title(channel_id: str) -> Optional[str]:
    """Тянет название канала из RSS feed (там оно в <title> верхнего уровня)."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _UA}) as c:
            r = await c.get(
                "https://www.youtube.com/feeds/videos.xml",
                params={"channel_id": channel_id},
            )
            if r.status_code != 200:
                return None
            # Первый <title>...</title> в feed — название канала
            m = re.search(r"<title>([^<]+)</title>", r.text)
            if m:
                return m.group(1).strip()
    except Exception as e:
        logger.warning("YT channel title %s failed: %s", channel_id, e)
    return None


# ---------- Последние видео ----------

async def fetch_latest_videos(channel_id: str, *, limit: int = 5) -> list[VideoEntry]:
    """Тянет до `limit` последних видео канала."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _UA}) as c:
            r = await c.get(
                "https://www.youtube.com/feeds/videos.xml",
                params={"channel_id": channel_id},
            )
            if r.status_code != 200:
                return []
            xml = r.text
            # Парсим простым regex (надёжно для RSS YouTube)
            entries: list[VideoEntry] = []
            # Каждый <entry>...</entry>
            for match in re.finditer(r"<entry>(.*?)</entry>", xml, re.DOTALL):
                block = match.group(1)
                vid_m = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", block)
                t_m = re.search(r"<title>([^<]+)</title>", block)
                p_m = re.search(r"<published>([^<]+)</published>", block)
                if not vid_m:
                    continue
                vid = vid_m.group(1)
                entries.append(VideoEntry(
                    video_id=vid,
                    title=(t_m.group(1) if t_m else "").strip(),
                    published=(p_m.group(1) if p_m else ""),
                    url=f"https://www.youtube.com/watch?v={vid}",
                ))
                if len(entries) >= limit:
                    break
            return entries
    except Exception as e:
        logger.warning("YT RSS fetch %s failed: %s", channel_id, e)
        return []
