"""Многоисточниковый поиск YouTube-id трейлера.

Стратегия (по убыванию приоритета и качества):

1. **TMDB** — если есть TMDB_API_KEY. Лучший источник для известных
   сериалов и фильмов: 90%+ покрытие, часто прямой русский трейлер
   (videos с iso_639_1='ru'). Идём через find by imdb_id, а если нет —
   через search.

2. **YouTube Data API v3** — если есть YOUTUBE_API_KEY. Поиск
   "<название> русский трейлер <год>" → первый videoId. 10К запросов
   в день бесплатно (1 search = 100 units).

3. **Piped** — без ключа. Публичные инстансы (zaggy.nl, kavin.rocks
   и др.) проксируют YouTube API. Падают периодически — пытаемся
   несколько по очереди.

4. **YouTube search URL fallback** — если ничего не нашли, возвращаем
   ссылку на страницу поиска YouTube. Telegram отрендерит preview,
   юзер выберет нужный сам. Никогда не пусто.

Результат: либо youtube_id (11 символов), либо None (если совсем ничего).
build_youtube_search_url(title) даёт fallback URL отдельно.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Публичные Piped инстансы — порядок: чаще онлайн первыми
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.privacydev.net",
    "https://pipedapi.r4fo.com",
]

TMDB_BASE = "https://api.themoviedb.org/3"


def _ru_query(title: str, year: Optional[int]) -> str:
    """Поисковый запрос для русского трейлера."""
    q = f"{title} русский трейлер"
    if year:
        q += f" {year}"
    return q


def _extract_youtube_id(url_or_id: str) -> Optional[str]:
    """Из URL или сырого id выдрать 11-символьный YouTube id."""
    if not url_or_id:
        return None
    # Просто сырой id
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url_or_id)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url_or_id)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{11})", url_or_id)
    if m:
        return m.group(1)
    return None


def build_youtube_search_url(title: str, year: Optional[int] = None) -> str:
    """URL страницы поиска YouTube — Telegram отрисует preview."""
    q = urllib.parse.quote_plus(_ru_query(title, year))
    return f"https://www.youtube.com/results?search_query={q}"


class TrailerFinder:
    """Ищет YouTube-id трейлера через несколько источников. Stateless."""

    def __init__(
        self,
        *,
        tmdb_api_key: Optional[str] = None,
        youtube_api_key: Optional[str] = None,
        timeout: float = 8.0,
    ) -> None:
        self._tmdb_key = (tmdb_api_key or os.getenv("TMDB_API_KEY", "")).strip() or None
        self._yt_key = (youtube_api_key or os.getenv("YOUTUBE_API_KEY", "")).strip() or None
        self._timeout = timeout

    # ---------- Источник 1: TMDB ----------

    async def from_tmdb(
        self,
        *,
        title: str,
        year: Optional[int],
        imdb_id: Optional[str] = None,
        tmdb_id: Optional[int] = None,
        is_series: bool = True,
    ) -> Optional[str]:
        """Через TMDB. Идёт в /movie/{id}/videos и /tv/{id}/videos.
        Берёт ru, потом en. Возвращает YouTube id."""
        if not self._tmdb_key:
            return None
        async with httpx.AsyncClient(timeout=self._timeout, base_url=TMDB_BASE) as c:
            params_base = {"api_key": self._tmdb_key}
            resolved_id = tmdb_id
            media_type = "tv" if is_series else "movie"

            # Если есть IMDb id — резолвим точный tmdb_id через /find
            if not resolved_id and imdb_id:
                try:
                    r = await c.get(
                        f"/find/{imdb_id}",
                        params={**params_base, "external_source": "imdb_id"},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("tv_results"):
                            resolved_id = data["tv_results"][0]["id"]
                            media_type = "tv"
                        elif data.get("movie_results"):
                            resolved_id = data["movie_results"][0]["id"]
                            media_type = "movie"
                except Exception as e:
                    logger.warning("TMDB find by imdb failed: %s", e)

            # Если всё ещё нет id — search по названию
            if not resolved_id:
                try:
                    r = await c.get(
                        f"/search/{media_type}",
                        params={**params_base, "query": title, "year": year or "", "language": "ru-RU"},
                    )
                    if r.status_code == 200:
                        results = r.json().get("results") or []
                        if not results and media_type == "tv":
                            # Попробуем как фильм
                            r = await c.get(
                                "/search/movie",
                                params={**params_base, "query": title, "year": year or "", "language": "ru-RU"},
                            )
                            if r.status_code == 200:
                                results = r.json().get("results") or []
                                if results:
                                    media_type = "movie"
                        if results:
                            resolved_id = results[0]["id"]
                except Exception as e:
                    logger.warning("TMDB search failed: %s", e)

            if not resolved_id:
                return None

            # Получаем videos. Сначала русские, потом всё остальное (en — основной).
            for lang in ("ru", "en", None):
                try:
                    params = {**params_base}
                    if lang:
                        params["language"] = f"{lang}-{lang.upper() if lang != 'en' else 'US'}"
                    r = await c.get(f"/{media_type}/{resolved_id}/videos", params=params)
                    if r.status_code != 200:
                        continue
                    videos = r.json().get("results") or []
                    # Сортируем: Trailer > Teaser; YouTube > Vimeo; official=True приоритет
                    def _score(v: dict) -> int:
                        s = 0
                        if v.get("site") == "YouTube":
                            s += 10
                        if (v.get("type") or "").lower() == "trailer":
                            s += 5
                        if v.get("official"):
                            s += 3
                        if (v.get("iso_639_1") or "").lower() == "ru":
                            s += 20
                        return -s

                    videos.sort(key=_score)
                    for v in videos:
                        if v.get("site") == "YouTube" and v.get("key"):
                            return v["key"]
                except Exception as e:
                    logger.warning("TMDB videos failed for %s/%s: %s", media_type, resolved_id, e)
        return None

    # ---------- Источник 2: YouTube Data API v3 ----------

    async def from_youtube_api(self, *, title: str, year: Optional[int]) -> Optional[str]:
        if not self._yt_key:
            return None
        q = _ru_query(title, year)
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "key": self._yt_key,
            "q": q,
            "part": "snippet",
            "type": "video",
            "maxResults": 5,
            "relevanceLanguage": "ru",
            "videoEmbeddable": "true",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                r = await c.get(url, params=params)
                if r.status_code != 200:
                    logger.warning("YouTube API %s: %s", r.status_code, r.text[:200])
                    return None
                items = r.json().get("items") or []
                for it in items:
                    vid = (it.get("id") or {}).get("videoId")
                    if vid:
                        return vid
        except Exception as e:
            logger.warning("YouTube API failed: %s", e)
        return None

    # ---------- Источник 3: Piped (без ключа) ----------

    async def from_piped(self, *, title: str, year: Optional[int]) -> Optional[str]:
        q = _ru_query(title, year)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            for instance in PIPED_INSTANCES:
                try:
                    r = await c.get(
                        f"{instance}/search",
                        params={"q": q, "filter": "videos"},
                    )
                    if r.status_code != 200:
                        continue
                    items = r.json().get("items") or []
                    for it in items[:5]:
                        url = it.get("url") or ""  # формат "/watch?v=ID"
                        m = re.search(r"v=([A-Za-z0-9_-]{11})", url)
                        if m:
                            return m.group(1)
                except Exception as e:
                    logger.warning("Piped %s failed: %s", instance, e)
                    continue
        return None

    # ---------- Главный метод ----------

    async def find(
        self,
        *,
        title: str,
        year: Optional[int] = None,
        imdb_id: Optional[str] = None,
        tmdb_id: Optional[int] = None,
        is_series: bool = True,
        kp_trailer_youtube_id: Optional[str] = None,
    ) -> Optional[str]:
        """Возвращает YouTube id или None.

        Порядок: уже знаем из KP → TMDB → YouTube Data API → Piped.
        """
        if kp_trailer_youtube_id:
            return kp_trailer_youtube_id

        # 1. TMDB
        if self._tmdb_key:
            try:
                yt_id = await self.from_tmdb(
                    title=title, year=year, imdb_id=imdb_id,
                    tmdb_id=tmdb_id, is_series=is_series,
                )
                if yt_id:
                    logger.info("Trailer for %s via TMDB: %s", title, yt_id)
                    return yt_id
            except Exception as e:
                logger.warning("TMDB stage failed: %s", e)

        # 2. YouTube Data API
        if self._yt_key:
            try:
                yt_id = await self.from_youtube_api(title=title, year=year)
                if yt_id:
                    logger.info("Trailer for %s via YouTube API: %s", title, yt_id)
                    return yt_id
            except Exception as e:
                logger.warning("YouTube API stage failed: %s", e)

        # 3. Piped
        try:
            yt_id = await self.from_piped(title=title, year=year)
            if yt_id:
                logger.info("Trailer for %s via Piped: %s", title, yt_id)
                return yt_id
        except Exception as e:
            logger.warning("Piped stage failed: %s", e)

        return None
