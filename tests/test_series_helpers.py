"""Тесты для bot/handlers/_series_helpers.py — чистые функции."""

from __future__ import annotations

from types import SimpleNamespace

from bot.handlers._series_helpers import (
    STATUS_LABELS,
    details_to_series_dict,
    format_caption,
)
from bot.services.kinopoisk import KPDetails


def _series(**overrides):
    """Лёгкий стаб для модели Series — мы используем только атрибуты."""
    base = dict(
        id=1, kp_id=42, title_ru="Severance", title_en="Severance",
        year=2022, poster_url=None, description_ru=None,
        genres="драма, триллер", rating_kp=8.5, rating_imdb=8.7,
        seasons=2, status_kp="completed",
        trailer_youtube_id=None, trailer_file_id=None, trailer_language=None,
        watch_options_json=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------- format_caption ----------

def test_format_caption_basic():
    s = _series()
    caption = format_caption(s)
    assert "Severance" in caption
    assert "(2022)" in caption
    assert "⭐ КП 8.5" in caption
    assert "IMDb 8.7" in caption
    assert "📺 2 сез." in caption
    assert "kinopoisk.ru/film/42/" in caption


def test_format_caption_with_status():
    s = _series()
    caption = format_caption(s, status="want")
    assert STATUS_LABELS["want"] in caption


def test_format_caption_trims_long_description():
    long_desc = "A" * 1000
    s = _series(description_ru=long_desc)
    caption = format_caption(s)
    assert "…" in caption
    assert len(caption) < 1500  # не разрослось до полной длины


def test_format_caption_skips_same_titles():
    s = _series(title_ru="Severance", title_en="Severance")
    caption = format_caption(s)
    # Не должно быть дубля "Severance / Severance"
    assert caption.count("Severance") == 1


def test_format_caption_watch_options_from_json():
    s = _series(watch_options_json='[["Кинопоиск HD", "https://hd.kinopoisk.ru/x"]]')
    caption = format_caption(s)
    assert "Кинопоиск HD" in caption
    assert "hd.kinopoisk.ru/x" in caption


def test_format_caption_note_appended():
    s = _series()
    caption = format_caption(s, note="зашло обоим")
    assert "зашло обоим" in caption


# ---------- details_to_series_dict ----------

def test_details_to_series_dict_basic():
    d = KPDetails(
        kp_id=42, title_ru="Severance", title_en=None, year=2022,
        description_ru="Mark works at...", poster_url="https://p/u",
        genres=["drama", "thriller"], rating_kp=8.5, rating_imdb=8.7,
        seasons=2, status_kp="completed", is_series=True,
        trailers=["https://youtu.be/abcdefghijk"],
        best_trailer_language="ru",
        watch_options=[("KP HD", "https://kp/x")],
    )
    result = details_to_series_dict(d)
    assert result["kp_id"] == 42
    assert result["genres"] == "drama, thriller"
    assert result["trailer_youtube_id"] == "abcdefghijk"
    assert result["trailer_language"] == "ru"
    assert "KP HD" in result["watch_options_json"]


def test_details_to_series_dict_empty_optional_fields():
    d = KPDetails(
        kp_id=1, title_ru="X", title_en=None, year=None,
        description_ru=None, poster_url=None,
    )
    result = details_to_series_dict(d)
    assert result["genres"] is None
    assert result["trailer_youtube_id"] is None
    assert result["trailer_language"] is None
    assert result["watch_options_json"] is None
