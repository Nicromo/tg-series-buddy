"""Клиент kinopoisk.dev API.

Документация: https://kinopoisk.dev/documentation
Авторизация: header X-API-KEY.
Базовый URL: https://api.kinopoisk.dev/v1.4

Возвращает данные о сериалах: название, постер, рейтинги (КП/IMDb),
описание, жанры, сезоны, трейлеры (часто сразу русские).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

DEFAULT_API_BASE = "https://api.kinopoisk.dev/v1.4"
# Альтернатива: https://api.poiskkino.dev/v1.4 (если используется ПоискКино)


@dataclass
class KPSearchHit:
    kp_id: int
    title_ru: str
    title_en: Optional[str]
    year: Optional[int]
    poster_url: Optional[str]
    short_description: Optional[str]
    rating_kp: Optional[float]


@dataclass
class KPEpisode:
    number: int
    name: Optional[str]
    air_date: Optional[str]  # "DD.MM.YYYY" или None


@dataclass
class KPSeason:
    number: int
    episodes_count: Optional[int]
    air_date: Optional[str]  # "DD.MM.YYYY" дата начала сезона
    episodes: list[KPEpisode]


@dataclass
class KPDetails:
    kp_id: int
    title_ru: str
    title_en: Optional[str]
    year: Optional[int]
    description_ru: Optional[str]
    poster_url: Optional[str]
    genres: list[str] = field(default_factory=list)
    rating_kp: Optional[float] = None
    rating_imdb: Optional[float] = None
    seasons: Optional[int] = None
    status_kp: Optional[str] = None
    is_series: bool = True

    # Трейлеры: список URL (часто YouTube), русские в приоритете
    trailers: list[str] = field(default_factory=list)

    # Язык лучшего (первого после сортировки) трейлера: "ru" | "en" | None
    best_trailer_language: Optional[str] = None

    # Где смотреть: [(имя сервиса, url)]
    watch_options: list[tuple] = field(default_factory=list)

    # External IDs — нужны для поиска трейлеров в TMDB и др.
    imdb_id: Optional[str] = None  # формат "tt1234567"
    tmdb_id: Optional[int] = None

    # Премьеры (ISO даты как строки, нормализованные в DD.MM.YYYY)
    premiere_world: Optional[str] = None
    premiere_russia: Optional[str] = None

    @property
    def best_trailer_youtube_id(self) -> Optional[str]:
        """Извлекаем YouTube-id из первой подходящей ссылки."""
        for url in self.trailers:
            if not url:
                continue
            yt_id = _extract_youtube_id(url)
            if yt_id:
                return yt_id
        return None

    @property
    def best_trailer_url(self) -> Optional[str]:
        """YouTube URL первого подходящего трейлера (для отправки текстом)."""
        yt_id = self.best_trailer_youtube_id
        if yt_id:
            return f"https://www.youtube.com/watch?v={yt_id}"
        # Если KP отдал не-YouTube — возвращаем первую ссылку как есть
        return self.trailers[0] if self.trailers else None


def _trailer_is_russian(name: str) -> bool:
    """True если имя трейлера похоже на русскую озвучку."""
    name = name.lower()
    return "рус" in name or " ru " in f" {name} " or name.startswith("ru ")


def _trailer_score(t: dict) -> int:
    """Score для сортировки трейлеров: чем меньше, тем выше в списке.
    Русские → лучше; «трейлер»/«trailer» → лучше; «тизер»/«teaser» → так себе.
    """
    name = (t.get("name") or "").lower()
    score = 0
    if _trailer_is_russian(name):
        score += 10
    if "трейлер" in name or "trailer" in name:
        score += 5
    if "тизер" in name or "teaser" in name:
        score += 2
    return -score


def _extract_youtube_id(url: str) -> Optional[str]:
    """Из любой ссылки YouTube вытаскиваем 11-символьный id."""
    if not url:
        return None
    # https://www.youtube.com/watch?v=XXXX
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    # https://youtu.be/XXXX
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    # https://www.youtube.com/embed/XXXX
    m = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    return None


class KinopoiskClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_API_BASE,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            base_url=base_url,
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- Поиск ----------

    async def search(self, query: str, *, limit: int = 5) -> list[KPSearchHit]:
        """Поиск по названию, отдаёт первые `limit` результатов.

        Сериалы и фильмы вперемешку — фильтруем сериалы на стороне клиента.
        """
        resp = await self._client.get(
            "/movie/search",
            params={"query": query, "limit": limit * 3, "page": 1},
        )
        resp.raise_for_status()
        docs = resp.json().get("docs", [])
        hits: list[KPSearchHit] = []
        for d in docs:
            # Берём и фильмы, и сериалы (без фильтра по типу)
            poster = (d.get("poster") or {}).get("url") or (d.get("poster") or {}).get("previewUrl")
            rating_kp = (d.get("rating") or {}).get("kp")
            hits.append(
                KPSearchHit(
                    kp_id=int(d["id"]),
                    title_ru=d.get("name") or d.get("alternativeName") or "?",
                    title_en=d.get("alternativeName"),
                    year=d.get("year"),
                    poster_url=poster,
                    short_description=d.get("shortDescription") or d.get("description"),
                    rating_kp=float(rating_kp) if rating_kp else None,
                )
            )
            if len(hits) >= limit:
                break
        return hits


    async def get_upcoming_series(self, *, year: Optional[int] = None, genres: Optional[list[str]] = None, limit: int = 10) -> list[KPSearchHit]:
        """Возвращает грядущие/недавние сериалы. Фильтр по году и жанрам."""
        params = {
            "limit": str(limit),
            "page": "1",
            "isSeries": "true",
            "sortField": "year",
            "sortType": "-1",
        }
        if year:
            params["year"] = str(year)
        if genres:
            for g in genres[:3]:
                params["genres.name"] = g  # multiple support via repeated key is API-specific
        try:
            resp = await self._client.get("/movie", params=params)
            resp.raise_for_status()
            docs = resp.json().get("docs", [])
        except Exception as e:
            return []
        hits: list[KPSearchHit] = []
        for d in docs:
            t = (d.get("type") or "").lower()
            if not (d.get("isSeries") is True or "series" in t or t == "anime"):
                continue
            poster = (d.get("poster") or {}).get("url") or (d.get("poster") or {}).get("previewUrl")
            rating_kp = (d.get("rating") or {}).get("kp")
            hits.append(KPSearchHit(
                kp_id=int(d["id"]),
                title_ru=d.get("name") or d.get("alternativeName") or "?",
                title_en=d.get("alternativeName"),
                year=d.get("year"),
                poster_url=poster,
                short_description=d.get("shortDescription") or d.get("description"),
                rating_kp=float(rating_kp) if rating_kp else None,
            ))
        return hits

    # ---------- Детали ----------

    async def get_top_by_genre(
        self, genre: str, *, is_series: Optional[bool] = None, limit: int = 10,
    ) -> list[KPSearchHit]:
        """Топ фильмов/сериалов по жанру, сортировка по rating.kp DESC."""
        params: dict[str, str] = {
            "limit": str(limit),
            "page": "1",
            "sortField": "rating.kp",
            "sortType": "-1",
            "genres.name": genre,
            "rating.kp": "7-10",  # отсекаем низкорейтинговые
        }
        if is_series is True:
            params["isSeries"] = "true"
        elif is_series is False:
            params["isSeries"] = "false"
        try:
            resp = await self._client.get("/movie", params=params)
            resp.raise_for_status()
            docs = resp.json().get("docs", [])
        except Exception:
            return []
        hits: list[KPSearchHit] = []
        for d in docs:
            poster = (d.get("poster") or {}).get("url") or (d.get("poster") or {}).get("previewUrl")
            rating_kp = (d.get("rating") or {}).get("kp")
            hits.append(KPSearchHit(
                kp_id=int(d["id"]),
                title_ru=d.get("name") or d.get("alternativeName") or "?",
                title_en=d.get("alternativeName"),
                year=d.get("year"),
                poster_url=poster,
                short_description=d.get("shortDescription") or d.get("description"),
                rating_kp=float(rating_kp) if rating_kp else None,
            ))
        return hits

    async def get_seasons(self, kp_id: int) -> list[KPSeason]:
        """Возвращает список сезонов с эпизодами и датами выхода.
        Для продолжающихся сериалов даты эпизодов могут быть в будущем
        (KP знает расписание заранее).
        """
        try:
            resp = await self._client.get(
                "/season",
                params={"movieId": str(kp_id), "limit": "50"},
            )
            if resp.status_code != 200:
                return []
            docs = resp.json().get("docs", []) or []
        except Exception:
            return []

        def _fmt(iso: Optional[str]) -> Optional[str]:
            if not iso:
                return None
            try:
                y, m, dd = iso[:10].split("-")
                return f"{dd}.{m}.{y}"
            except Exception:
                return None

        seasons: list[KPSeason] = []
        for d in docs:
            num = d.get("number")
            if not isinstance(num, int):
                continue
            episodes_raw = d.get("episodes") or []
            episodes = [
                KPEpisode(
                    number=e.get("number") or i + 1,
                    name=(e.get("name") or "").strip() or None,
                    air_date=_fmt(e.get("airDate")),
                )
                for i, e in enumerate(episodes_raw)
            ]
            seasons.append(KPSeason(
                number=num,
                episodes_count=d.get("episodesCount") or len(episodes) or None,
                air_date=_fmt(d.get("airDate")),
                episodes=episodes,
            ))
        # KP возвращает сезоны в произвольном порядке — сортируем по номеру
        seasons.sort(key=lambda s: s.number)
        return seasons

    async def get_details(self, kp_id: int) -> KPDetails:
        resp = await self._client.get(f"/movie/{kp_id}")
        resp.raise_for_status()
        d = resp.json()

        poster = (d.get("poster") or {}).get("url") or (d.get("poster") or {}).get("previewUrl")
        rating = d.get("rating") or {}
        genres = [g["name"] for g in (d.get("genres") or []) if g.get("name")]

        # Сезоны: kinopoisk.dev возвращает seasonsInfo или totalSeasons
        seasons = None
        if d.get("seasonsInfo"):
            seasons = len(d["seasonsInfo"])
        elif d.get("totalSeasons"):
            seasons = int(d["totalSeasons"])

        # Трейлеры: сортировка по _trailer_score
        trailers_raw = ((d.get("videos") or {}).get("trailers") or [])
        trailers_sorted = sorted(trailers_raw, key=_trailer_score)
        trailers = [t["url"] for t in trailers_sorted if t.get("url")]

        # Язык лучшего трейлера — по имени первого после сортировки
        best_trailer_language: Optional[str] = None
        if trailers_sorted:
            best_name = trailers_sorted[0].get("name") or ""
            best_trailer_language = "ru" if _trailer_is_russian(best_name) else "en"

        ext = d.get("externalId") or {}
        imdb_id = (ext.get("imdb") or "").strip() or None
        tmdb_id_raw = ext.get("tmdb")
        tmdb_id = int(tmdb_id_raw) if tmdb_id_raw else None

        # Премьеры
        prem = d.get("premiere") or {}
        def _fmt_date(iso: Optional[str]) -> Optional[str]:
            """Преобразует ISO дату вида '2024-01-17T00:00:00.000Z' в '17.01.2024'."""
            if not iso:
                return None
            try:
                core = iso[:10]  # YYYY-MM-DD
                y, m, dd = core.split("-")
                return f"{dd}.{m}.{y}"
            except Exception:
                return None
        premiere_world = _fmt_date(prem.get("world"))
        premiere_russia = _fmt_date(prem.get("russia"))

        watch_options_raw = ((d.get("watchability") or {}).get("items") or [])
        watch_options = []
        for w in watch_options_raw[:6]:
            name = (w.get("name") or "").strip()
            url = (w.get("url") or "").strip()
            if name and url:
                watch_options.append((name, url))

        return KPDetails(
            kp_id=int(d["id"]),
            title_ru=d.get("name") or d.get("alternativeName") or "?",
            title_en=d.get("alternativeName"),
            year=d.get("year"),
            description_ru=d.get("description") or d.get("shortDescription"),
            poster_url=poster,
            genres=genres,
            rating_kp=float(rating.get("kp")) if rating.get("kp") else None,
            rating_imdb=float(rating.get("imdb")) if rating.get("imdb") else None,
            seasons=seasons,
            status_kp=d.get("status"),
            is_series=bool(d.get("isSeries") or ((d.get("type") or "").lower().endswith("series"))),
            trailers=trailers,
            best_trailer_language=best_trailer_language,
            watch_options=watch_options,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            premiere_world=premiere_world,
            premiere_russia=premiere_russia,
        )
