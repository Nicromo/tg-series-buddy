"""Многоисточниковый поиск URL трейлера.

Стратегия (по убыванию приоритета и качества):

1. **TMDB** — если есть TMDB_API_KEY. Лучший источник для известных
   сериалов и фильмов: 90%+ покрытие, часто прямой русский трейлер
   (videos с iso_639_1='ru'). Идём через find by imdb_id, а если нет —
   через search.

2. **YouTube Data API v3** — если есть YOUTUBE_API_KEY. Поиск
   "<название> русский трейлер <год>" → первый videoId. 10К запросов
   в день бесплатно (1 search = 100 units).

3. **Piped** — без ключа. Публичные инстансы проксируют YouTube API.
   Падают периодически — пытаемся несколько по очереди.

4. **Invidious** — без ключа, другой набор публичных инстансов.
   Дополняет Piped.

5. **RuTube** — без ключа. Публичное API rutube.ru/api/search/video/.
   Отдаёт RuTube URL (не YouTube). Часто именно русские трейлеры
   как для русского, так и для зарубежного контента. Telegram сам
   отрендерит preview по URL.

6. **DuckDuckGo HTML search** — без ключа. Парсим HTML страницы
   результатов с фильтром site:youtube.com, достаём первый
   youtube.com/watch?v=... URL.

7. **YouTube search URL fallback** — если ничего не нашли, возвращаем
   ссылку на страницу поиска YouTube. Telegram отрендерит preview,
   юзер выберет нужный сам. Никогда не пусто.

Результат: URL (любой источник) или None.
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


# Публичные Piped инстансы — порядок: чаще онлайн первыми.
# Списки устаревают — если все падают, актуальные см. на piped-instances.kavin.rocks
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.privacydev.net",
    "https://pipedapi.r4fo.com",
    "https://pipedapi.darkness.services",
    "https://pipedapi.in.projectsegfau.lt",
    "https://piapi.ggtyler.dev",
]

# Invidious — аналог Piped. Актуальный список: api.invidious.io
INVIDIOUS_INSTANCES = [
    "https://invidious.privacyredirect.com",
    "https://yewtu.be",
    "https://invidious.nerdvpn.de",
    "https://inv.nadeko.net",
    "https://invidious.f5.si",
    "https://invidious.materialio.us",
    "https://iv.melmac.space",
]

# User-Agent — DuckDuckGo HTML банит default httpx
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

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
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": UA_BROWSER},
        ) as c:
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

    # ---------- Источник 4: Invidious (без ключа) ----------

    async def from_invidious(self, *, title: str, year: Optional[int]) -> Optional[str]:
        q = _ru_query(title, year)
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": UA_BROWSER},
        ) as c:
            for instance in INVIDIOUS_INSTANCES:
                try:
                    r = await c.get(
                        f"{instance}/api/v1/search",
                        params={"q": q, "type": "video", "region": "RU"},
                    )
                    if r.status_code != 200:
                        continue
                    items = r.json() or []
                    for it in items[:5]:
                        vid = it.get("videoId")
                        if vid and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                            return vid
                except Exception as e:
                    logger.warning("Invidious %s failed: %s", instance, e)
                    continue
        return None

    # ---------- Источник 4.5: RuTube (без ключа, отдаёт RU трейлеры) ----------

    async def from_rutube(self, *, title: str, year: Optional[int]) -> Optional[str]:
        """Поиск трейлера на RuTube. Возвращает RuTube URL (НЕ YouTube).
        Часто отдаёт именно русские трейлеры для русских и зарубежных названий.
        Telegram сам отрисует preview с тумбнейлом.
        """
        q = f"{title} русский трейлер"
        if year:
            q += f" {year}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": UA_BROWSER, "Accept": "application/json"},
            ) as c:
                r = await c.get(
                    "https://rutube.ru/api/search/video/",
                    params={"query": q, "format": "json"},
                )
                if r.status_code != 200:
                    logger.warning("RuTube status %s", r.status_code)
                    return None
                results = (r.json().get("results") or [])[:5]
                # Берём результат у которого в title есть «трейлер» (отсекаем подкасты/обзоры/реакции)
                trailer_words = ("трейлер", "тизер", "trailer", "teaser")
                bad_words = ("обзор", "разбор", "реакция", "review", "podcast", "подкаст")
                best = None
                for it in results:
                    t = (it.get("title") or "").lower()
                    url = it.get("video_url") or ""
                    if not url:
                        continue
                    has_trailer = any(w in t for w in trailer_words)
                    has_bad = any(w in t for w in bad_words)
                    if has_trailer and not has_bad:
                        return url  # первый хороший — берём
                    if best is None and not has_bad:
                        best = url
                return best
        except Exception as e:
            logger.warning("RuTube failed: %s", e)
        return None

    # ---------- Источник 5: DuckDuckGo HTML (без ключа) ----------

    async def _ddg_one(self, q: str) -> Optional[str]:
        """Один запрос к DDG HTML. Пытается извлечь YouTube id."""
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": UA_BROWSER},
            follow_redirects=True,
        ) as c:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": q})
        if r.status_code != 200:
            logger.warning("DDG status %s for q=%r", r.status_code, q[:80])
            return None
        html = r.text
        # Прямой и url-encoded варианты
        patterns = [
            r"youtube\.com%2Fwatch%3Fv%3D([A-Za-z0-9_-]{11})",
            r"youtube\.com/watch\?v=([A-Za-z0-9_-]{11})",
            r"youtu\.be%2F([A-Za-z0-9_-]{11})",
            r"youtu\.be/([A-Za-z0-9_-]{11})",
        ]
        for p in patterns:
            m = re.search(p, html)
            if m:
                return m.group(1)
        return None

    async def from_duckduckgo(
        self,
        *,
        title: str,
        year: Optional[int],
        title_en: Optional[str] = None,
    ) -> Optional[str]:
        """HTML парсинг DDG. Пробует несколько вариантов запроса:
        1. <title_ru> русский трейлер <year> site:youtube.com
        2. <title_en> trailer <year> site:youtube.com  (если есть title_en)
        3. <title_ru> trailer <year>  (без site: — менее точно)
        """
        queries = [_ru_query(title, year) + " site:youtube.com"]
        if title_en and title_en != title:
            en_q = f"{title_en} trailer"
            if year:
                en_q += f" {year}"
            queries.append(en_q + " site:youtube.com")
        queries.append(_ru_query(title, year))  # без site: фильтра

        for q in queries:
            try:
                yt_id = await self._ddg_one(q)
                if yt_id:
                    return yt_id
            except Exception as e:
                logger.warning("DDG query failed (%s): %s", q[:50], e)
        return None

    # ---------- Достать mp4 для send_video (встроенный плеер Telegram) ----------

    async def fetch_video_bytes(
        self,
        youtube_id: str,
        *,
        max_mb: int = 45,
        prefer_quality: tuple[str, ...] = ("480p", "360p", "240p", "720p"),
    ) -> Optional[bytes]:
        """Через Piped/Invidious /streams получить прямой mp4 URL и скачать
        bytes (≤ max_mb). Можно отправить через send_video → играет в чате.

        Возвращает None если ни один инстанс не отвечает, либо стрим > max_mb.
        """
        stream_url = await self._resolve_stream_url(youtube_id, prefer_quality)
        if not stream_url:
            return None
        max_bytes = max_mb * 1024 * 1024
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
                follow_redirects=True,
                headers={"User-Agent": UA_BROWSER},
            ) as c:
                # Сначала HEAD (если поддерживается) — проверить размер
                try:
                    head = await c.head(stream_url)
                    cl = head.headers.get("content-length")
                    if cl and int(cl) > max_bytes:
                        logger.info(
                            "Stream %s too big (%s MB), skip download",
                            youtube_id, int(cl) // 1024 // 1024,
                        )
                        return None
                except Exception:
                    pass  # HEAD не обязателен

                # GET с потоком — обрываем если превысили лимит
                async with c.stream("GET", stream_url) as resp:
                    if resp.status_code != 200:
                        logger.warning("Stream GET %s: %s", youtube_id, resp.status_code)
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            logger.info("Stream %s exceeded %d MB during download", youtube_id, max_mb)
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
        except Exception as e:
            logger.warning("fetch_video_bytes failed for %s: %s", youtube_id, e)
            return None

    async def _resolve_stream_url(
        self, youtube_id: str, prefer_quality: tuple[str, ...]
    ) -> Optional[str]:
        """Перебирает Piped и Invidious инстансы, ищет mp4-URL подходящего качества."""
        # 1) Piped: GET /streams/{id} → {"videoStreams": [...]}
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": UA_BROWSER},
        ) as c:
            for inst in PIPED_INSTANCES:
                try:
                    r = await c.get(f"{inst}/streams/{youtube_id}")
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    streams = data.get("videoStreams") or []
                    # Только muxed (видео+аудио вместе), не videoOnly
                    muxed = [
                        s for s in streams
                        if not s.get("videoOnly") and s.get("url")
                        and (s.get("format") or "").upper().startswith("MPEG")
                    ]
                    if not muxed:
                        # Fallback: любой не-videoOnly с url
                        muxed = [s for s in streams if not s.get("videoOnly") and s.get("url")]
                    for q in prefer_quality:
                        for s in muxed:
                            if s.get("quality") == q:
                                return s["url"]
                    # Если нет нужного качества — берём первый муxed
                    if muxed:
                        return muxed[0]["url"]
                except Exception as e:
                    logger.warning("Piped /streams %s failed: %s", inst, e)
                    continue

            # 2) Invidious: GET /api/v1/videos/{id} → {"formatStreams":[...]}
            for inst in INVIDIOUS_INSTANCES:
                try:
                    r = await c.get(f"{inst}/api/v1/videos/{youtube_id}")
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    # formatStreams — это уже muxed (видео+аудио вместе)
                    fmts = data.get("formatStreams") or []
                    for q in prefer_quality:
                        for s in fmts:
                            if s.get("qualityLabel") == q and s.get("url"):
                                return s["url"]
                    if fmts:
                        return fmts[0].get("url")
                except Exception as e:
                    logger.warning("Invidious /videos %s failed: %s", inst, e)
                    continue
        return None

    # ---------- Главный метод ----------

    async def find(
        self,
        *,
        title: str,
        year: Optional[int] = None,
        title_en: Optional[str] = None,
        imdb_id: Optional[str] = None,
        tmdb_id: Optional[int] = None,
        is_series: bool = True,
        kp_trailer_youtube_id: Optional[str] = None,
    ) -> Optional[str]:
        """Возвращает URL трейлера (любой источник) или None.

        Порядок: KP → TMDB → YouTube API → Piped → Invidious → RuTube →
        DuckDuckGo. Для YouTube-источников URL вида
        https://www.youtube.com/watch?v=<id>; для RuTube — rutube.ru/video/.../.
        Каждый keyless источник пробуем с title_ru и title_en.
        """
        if kp_trailer_youtube_id:
            return f"https://www.youtube.com/watch?v={kp_trailer_youtube_id}"

        def _yt_url(yt_id: str) -> str:
            return f"https://www.youtube.com/watch?v={yt_id}"

        # 1. TMDB
        if self._tmdb_key:
            try:
                yt_id = await self.from_tmdb(
                    title=title, year=year, imdb_id=imdb_id,
                    tmdb_id=tmdb_id, is_series=is_series,
                )
                if yt_id:
                    logger.info("Trailer for %s via TMDB: %s", title, yt_id)
                    return _yt_url(yt_id)
            except Exception as e:
                logger.warning("TMDB stage failed: %s", e)

        # 2. YouTube Data API
        if self._yt_key:
            for q_title in (t for t in (title, title_en) if t):
                try:
                    yt_id = await self.from_youtube_api(title=q_title, year=year)
                    if yt_id:
                        logger.info("Trailer for %s via YouTube API (%s): %s", title, q_title, yt_id)
                        return _yt_url(yt_id)
                except Exception as e:
                    logger.warning("YouTube API stage failed for %s: %s", q_title, e)

        # 3. Piped
        for q_title in (t for t in (title, title_en) if t):
            try:
                yt_id = await self.from_piped(title=q_title, year=year)
                if yt_id:
                    logger.info("Trailer for %s via Piped (%s): %s", title, q_title, yt_id)
                    return _yt_url(yt_id)
            except Exception as e:
                logger.warning("Piped stage failed for %s: %s", q_title, e)

        # 4. Invidious
        for q_title in (t for t in (title, title_en) if t):
            try:
                yt_id = await self.from_invidious(title=q_title, year=year)
                if yt_id:
                    logger.info("Trailer for %s via Invidious (%s): %s", title, q_title, yt_id)
                    return _yt_url(yt_id)
            except Exception as e:
                logger.warning("Invidious stage failed for %s: %s", q_title, e)

        # 5. RuTube — отдаёт точные русские трейлеры для русских и
        # зарубежных названий. Telegram сам отрисует preview.
        try:
            rutube_url = await self.from_rutube(title=title, year=year)
            if rutube_url:
                logger.info("Trailer for %s via RuTube: %s", title, rutube_url)
                return rutube_url
        except Exception as e:
            logger.warning("RuTube stage failed: %s", e)

        # 6. DuckDuckGo HTML
        try:
            yt_id = await self.from_duckduckgo(title=title, year=year, title_en=title_en)
            if yt_id:
                logger.info("Trailer for %s via DuckDuckGo: %s", title, yt_id)
                return _yt_url(yt_id)
        except Exception as e:
            logger.warning("DuckDuckGo stage failed: %s", e)

        return None
