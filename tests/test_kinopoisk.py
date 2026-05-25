"""Тесты для bot/services/kinopoisk.py — чистые функции без сети."""

from __future__ import annotations

from bot.services.kinopoisk import (
    KPDetails,
    _extract_youtube_id,
    _trailer_is_russian,
    _trailer_score,
)


# ---------- _trailer_is_russian ----------

def test_trailer_is_russian_explicit_word():
    assert _trailer_is_russian("Русский трейлер")
    assert _trailer_is_russian("РУСС")
    assert _trailer_is_russian("Дубляж русский")


def test_trailer_is_russian_ru_marker():
    assert _trailer_is_russian("ru dubbed")
    assert _trailer_is_russian("trailer ru official")


def test_trailer_is_russian_english_only():
    assert not _trailer_is_russian("Official Trailer")
    assert not _trailer_is_russian("Teaser")
    assert not _trailer_is_russian("")


# ---------- _trailer_score (lower = better) ----------

def test_trailer_score_russian_beats_english():
    ru = _trailer_score({"name": "Русский трейлер"})
    en = _trailer_score({"name": "Official Trailer"})
    assert ru < en


def test_trailer_score_full_trailer_beats_teaser():
    full = _trailer_score({"name": "Official Trailer"})
    teaser = _trailer_score({"name": "Official Teaser"})
    assert full < teaser


def test_trailer_score_sorting():
    items = [
        {"name": "Teaser"},
        {"name": "Русский трейлер"},
        {"name": "English Trailer"},
        {"name": "no idea"},
    ]
    sorted_items = sorted(items, key=_trailer_score)
    # Сначала русский, потом english trailer, потом teaser, потом "no idea"
    assert sorted_items[0]["name"] == "Русский трейлер"
    assert sorted_items[1]["name"] == "English Trailer"


def test_trailer_score_missing_name():
    assert _trailer_score({}) == 0
    assert _trailer_score({"name": None}) == 0


# ---------- _extract_youtube_id ----------

def test_extract_youtube_id_watch():
    assert _extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_youtube_id_short():
    assert _extract_youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_youtube_id_embed():
    assert _extract_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_youtube_id_watch_with_params():
    assert _extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s") == "dQw4w9WgXcQ"


def test_extract_youtube_id_not_youtube():
    assert _extract_youtube_id("https://vimeo.com/123456") is None
    assert _extract_youtube_id("") is None
    assert _extract_youtube_id("not a url") is None


# ---------- KPDetails properties ----------

def test_kp_details_best_trailer_youtube_id_from_list():
    d = KPDetails(
        kp_id=1, title_ru="X", title_en=None, year=None,
        description_ru=None, poster_url=None,
        trailers=["https://youtu.be/abcdefghijk", "https://example.com"],
    )
    assert d.best_trailer_youtube_id == "abcdefghijk"


def test_kp_details_best_trailer_url():
    d = KPDetails(
        kp_id=1, title_ru="X", title_en=None, year=None,
        description_ru=None, poster_url=None,
        trailers=["https://youtu.be/abcdefghijk"],
    )
    assert d.best_trailer_url == "https://www.youtube.com/watch?v=abcdefghijk"


def test_kp_details_best_trailer_url_non_youtube_fallback():
    d = KPDetails(
        kp_id=1, title_ru="X", title_en=None, year=None,
        description_ru=None, poster_url=None,
        trailers=["https://vimeo.com/123"],
    )
    assert d.best_trailer_url == "https://vimeo.com/123"


def test_kp_details_no_trailers():
    d = KPDetails(
        kp_id=1, title_ru="X", title_en=None, year=None,
        description_ru=None, poster_url=None,
    )
    assert d.best_trailer_youtube_id is None
    assert d.best_trailer_url is None
