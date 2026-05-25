"""Тесты для bot/services/trailer_search.py — чистые функции (без сети)."""

from __future__ import annotations

from bot.services.trailer_search import (
    TrailerFinder,
    _extract_youtube_id,
    _ru_query,
    build_youtube_search_url,
)


# ---------- _extract_youtube_id (вариант с распознаванием сырого id) ----------

def test_extract_youtube_id_from_watch_url():
    assert _extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_youtube_id_from_short_url():
    assert _extract_youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_youtube_id_from_raw_id():
    assert _extract_youtube_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_youtube_id_from_embed():
    assert _extract_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_youtube_id_empty():
    assert _extract_youtube_id("") is None
    assert _extract_youtube_id("garbage") is None


# ---------- _ru_query ----------

def test_ru_query_with_year():
    q = _ru_query("Severance", 2022)
    assert "Severance" in q
    assert "русский трейлер" in q
    assert "2022" in q


def test_ru_query_no_year():
    q = _ru_query("Severance", None)
    assert q == "Severance русский трейлер"


# ---------- build_youtube_search_url ----------

def test_build_youtube_search_url_url_encoded():
    url = build_youtube_search_url("Очень странные дела", 2016)
    assert url.startswith("https://www.youtube.com/results?search_query=")
    # spaces -> +
    assert "+" in url
    # year embedded somewhere
    assert "2016" in url


def test_build_youtube_search_url_special_chars():
    url = build_youtube_search_url("A&B's show", None)
    # ampersand and apostrophe must be encoded so they don't break query
    assert "%26" in url or "%27" in url


# ---------- TrailerFinder.__init__ ----------

def test_trailer_finder_no_keys_when_env_empty(monkeypatch):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    f = TrailerFinder()
    assert f._tmdb_key is None
    assert f._yt_key is None


def test_trailer_finder_picks_up_env(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-x")
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-x")
    f = TrailerFinder()
    assert f._tmdb_key == "tmdb-x"
    assert f._yt_key == "yt-x"


def test_trailer_finder_explicit_keys_win_over_env(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "env-tmdb")
    f = TrailerFinder(tmdb_api_key="explicit")
    assert f._tmdb_key == "explicit"
