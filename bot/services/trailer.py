"""Поиск и скачивание трейлеров (приоритет — на русском).

Стратегия:
1. Если TMDB вернул русский трейлер (key + lang='ru') — скачиваем его с YouTube.
2. Если TMDB вернул только английский — пробуем найти русский через YouTube-поиск:
   "<название_ru> русский трейлер <год>".
3. Если поиск ничего не дал — скачиваем то, что есть от TMDB (англ).

yt-dlp вытягивает mp4 ≤ MAX_MB. Возвращаем путь к локальному файлу.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _build_ydl_opts(out_path: Path, max_mb: int) -> dict:
    """Опции yt-dlp: один файл mp4, ≤ max_mb, без объединения через ffmpeg."""
    # filesize<{max}M фильтр работает на этапе выбора формата, чтобы не качать большое
    return {
        "format": (
            f"best[ext=mp4][filesize<{max_mb}M]/"
            f"best[ext=mp4][height<=480]/"
            f"best[filesize<{max_mb}M]/best"
        ),
        "outtmpl": str(out_path),
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        # Ускоряем: один поток, без подсказок
        "concurrent_fragment_downloads": 1,
        "retries": 2,
        "socket_timeout": 20,
    }


async def _run_in_thread(func, *args, **kwargs):
    """Запускает blocking-функцию в отдельном потоке."""
    return await asyncio.to_thread(func, *args, **kwargs)


def _ydl_download(url: str, opts: dict) -> Optional[str]:
    """Синхронный вызов yt-dlp. Возвращает реальный путь скачанного файла."""
    # Импорт внутри — чтобы tests без yt-dlp не падали
    from yt_dlp import YoutubeDL  # type: ignore

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return None
            # Если был ytsearch — в info будет 'entries'
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            return ydl.prepare_filename(info)
    except Exception as e:  # noqa: BLE001
        logger.warning("yt-dlp failed for %s: %s", url, e)
        return None


async def download_youtube_trailer(
    youtube_id: str,
    out_dir: Path,
    max_mb: int,
    *,
    filename_hint: str = "trailer",
) -> Optional[Path]:
    """Скачивает видео по YouTube-id. Возвращает путь к файлу или None."""
    out_path = out_dir / f"{filename_hint}_{youtube_id}.%(ext)s"
    url = f"https://www.youtube.com/watch?v={youtube_id}"
    opts = _build_ydl_opts(out_path, max_mb)
    real_path = await _run_in_thread(_ydl_download, url, opts)
    if real_path and Path(real_path).exists():
        return Path(real_path)
    return None


async def search_and_download_ru_trailer(
    title_ru: str,
    year: Optional[int],
    out_dir: Path,
    max_mb: int,
) -> Optional[Path]:
    """YouTube-поиск '<название> русский трейлер <год>' и скачивание первого результата."""
    query = f"{title_ru} русский трейлер"
    if year:
        query += f" {year}"
    out_path = out_dir / f"trailer_search_{abs(hash(query)) % 10**10}.%(ext)s"
    opts = _build_ydl_opts(out_path, max_mb)
    real_path = await _run_in_thread(_ydl_download, f"ytsearch1:{query}", opts)
    if real_path and Path(real_path).exists():
        return Path(real_path)
    return None


async def fetch_best_trailer(
    *,
    title_ru: str,
    year: Optional[int],
    youtube_id: Optional[str],
    tmdb_language: Optional[str],
    out_dir: Path,
    max_mb: int,
) -> tuple[Optional[Path], str]:
    """Возвращает (путь к файлу, описание источника).

    Логика:
    - Если TMDB дал ru-трейлер → качаем его.
    - Иначе пробуем YouTube-поиск русского.
    - Иначе качаем то, что TMDB дал (en).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    if youtube_id and tmdb_language == "ru":
        path = await download_youtube_trailer(youtube_id, out_dir, max_mb)
        if path:
            return path, "TMDB (ru)"

    # Пробуем найти русский на YouTube
    path = await search_and_download_ru_trailer(title_ru, year, out_dir, max_mb)
    if path:
        return path, "YouTube поиск (ru)"

    # Fallback на исходный трейлер от TMDB (любой язык)
    if youtube_id:
        path = await download_youtube_trailer(youtube_id, out_dir, max_mb)
        if path:
            return path, f"TMDB ({tmdb_language or 'en'})"

    return None, "не найден"


async def find_trailer_tg_link(title_ru: str, year=None) -> "Optional[str]":
    """Ищет ссылку на пост с трейлером в публичных TG-каналах (t.me/s/...).

    Возвращает URL вида https://t.me/<channel>/<post_id> — Telegram сам отрисует превью с видео.
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
