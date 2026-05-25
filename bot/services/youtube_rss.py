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

    Защита от «не того канала»: YouTube вставляет на страницу channelId
    рекомендуемых каналов раньше основного. Поэтому:
    1. Сначала пробуем og:url meta (всегда главный канал).
    2. Потом «externalId» (это основной, рекомендации идут через channelId).
    3. Если и это пусто — берём САМЫЙ ЧАСТЫЙ channel_id на странице
       (основной канал упоминается десятки раз).
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
            ch_id: Optional[str] = None

            # 1) og:url наиболее надёжно — содержит canonical URL канала
            m = re.search(r'<meta property="og:url" content="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"', html)
            if m:
                ch_id = m.group(1)
            # 2) externalId в JSON — основной канал
            if not ch_id:
                m = re.search(r'"externalId":"(UC[A-Za-z0-9_-]{22})"', html)
                if m:
                    ch_id = m.group(1)
            # 3) canonical link
            if not ch_id:
                m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"', html)
                if m:
                    ch_id = m.group(1)
            # 4) Эвристика «самый частый channel_id» — основной канал
            # упоминается сильно чаще рекомендованных
            if not ch_id:
                from collections import Counter
                all_ids = re.findall(r'(UC[A-Za-z0-9_-]{22})', html)
                if all_ids:
                    ctr = Counter(all_ids)
                    top_id, top_count = ctr.most_common(1)[0]
                    # Берём только если он явно доминирует (хотя бы 5+ и в 3+ раза чаще второго)
                    if top_count >= 5:
                        second = ctr.most_common(2)[1][1] if len(ctr) > 1 else 0
                        if top_count >= 3 * second or second == 0:
                            ch_id = top_id
            if not ch_id:
                return None
            # Title — берём через robust-fetch (RSS + HTML с og:title).
            # На HTML-странице JSON-строки могут содержать \uXXXX —
            # парсим только через json.loads, никогда через unicode_escape.
            title = await fetch_channel_title_robust(ch_id)
            if not title:
                # Очень редкий случай — берём из текущей HTML страницы
                m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                if m:
                    import html as _html
                    title = _html.unescape(m.group(1).strip())
            return ChannelInfo(channel_id=ch_id, title=title or handle)
    except Exception as e:
        logger.warning("YT handle resolve %s failed: %s", handle, e)
        return None


def _looks_mojibake(s: str) -> bool:
    """Эвристика на сломанный UTF-8 (mojibake): «Ð», «Ñ» и компания."""
    if not s:
        return True
    bad_chars = "ÐÑ°±²³´µ¶·¸¹º»¼½¾¿ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÒÓÔÕÖ×ØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõö"
    bad = sum(1 for ch in s if ch in bad_chars)
    return bad >= 3 and bad / max(1, len(s)) > 0.3


async def _fetch_channel_title(channel_id: str) -> Optional[str]:
    """Тянет название канала из RSS feed (там оно в <title> верхнего уровня)."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _UA}) as c:
            r = await c.get(
                "https://www.youtube.com/feeds/videos.xml",
                params={"channel_id": channel_id},
            )
            if r.status_code != 200:
                logger.info("YT RSS title %s status %s", channel_id, r.status_code)
                return None
            # Структура: <feed><id/><channelId/><title>NAME</title>...
            # Первый <title> — это название канала
            m = re.search(r"<title>([^<]+)</title>", r.text)
            if m:
                title = m.group(1).strip()
                # RSS HTTP-headers говорят UTF-8 → httpx должен декодить правильно.
                # Если всё-таки сломалось — пробуем restore
                if _looks_mojibake(title):
                    try:
                        title = title.encode("latin-1").decode("utf-8")
                    except Exception:
                        pass
                return title
    except Exception as e:
        logger.warning("YT channel title %s failed: %s", channel_id, e)
    return None


async def fetch_channel_title_robust(channel_id: str) -> Optional[str]:
    """Несколько источников названия канала, возвращает первый годный
    (не None, не mojibake). Public — для использования в /subs автопочинке.

    1. RSS feed (наиболее надёжно)
    2. HTML страница канала /channel/UCxxx — meta property=og:title
    3. То же — meta name=title
    """
    # 1) RSS
    title = await _fetch_channel_title(channel_id)
    if title and not _looks_mojibake(title):
        return title

    # 2-3) HTML страница
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": _UA,
                "Cookie": _CONSENT_COOKIE,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
            follow_redirects=True,
        ) as c:
            r = await c.get(f"https://www.youtube.com/channel/{channel_id}")
            if r.status_code != 200:
                return title
            html = r.text
            # og:title — наиболее надёжный, отдаётся как UTF-8 строка без escape
            for pattern in (
                r'<meta property="og:title" content="([^"]+)"',
                r'<meta name="title" content="([^"]+)"',
            ):
                m = re.search(pattern, html)
                if m:
                    cand = m.group(1).strip()
                    # HTML-unescape для &amp; и т.п.
                    import html as _html
                    cand = _html.unescape(cand)
                    if cand and not _looks_mojibake(cand):
                        return cand
    except Exception as e:
        logger.warning("YT channel HTML title %s failed: %s", channel_id, e)
    return title


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
