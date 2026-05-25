"""Тесты для _parse_titles_bulk — разбор массивного ввода названий."""

from __future__ import annotations

from bot.handlers.series import _parse_titles_bulk


# ---------- Однострочные (не bulk) ----------

def test_single_title_returns_empty():
    assert _parse_titles_bulk("Severance") == []


def test_single_title_with_year_returns_empty():
    assert _parse_titles_bulk("Дюна 2024") == []


def test_short_comma_in_title_not_split():
    # «1, 2 Дюна» — короткие части, не разбиваем
    assert _parse_titles_bulk("X, 5") == []


# ---------- По новой строке ----------

def test_two_lines():
    result = _parse_titles_bulk("Severance\nStranger Things")
    assert result == ["Severance", "Stranger Things"]


def test_lines_with_numbers():
    text = "1. Severance\n2. Stranger Things\n3. Dark"
    result = _parse_titles_bulk(text)
    assert result == ["Severance", "Stranger Things", "Dark"]


def test_lines_with_paren_numbers():
    text = "1) Severance\n2) Dark"
    result = _parse_titles_bulk(text)
    assert result == ["Severance", "Dark"]


def test_lines_with_bullets():
    text = "- Severance\n- Stranger Things\n• Dark"
    result = _parse_titles_bulk(text)
    assert result == ["Severance", "Stranger Things", "Dark"]


def test_lines_with_empty_lines_skipped():
    text = "Severance\n\n\nDark\n"
    result = _parse_titles_bulk(text)
    assert result == ["Severance", "Dark"]


# ---------- По запятой / точке с запятой ----------

def test_comma_separated():
    result = _parse_titles_bulk("Severance, Stranger Things, Dark")
    assert result == ["Severance", "Stranger Things", "Dark"]


def test_semicolon_separated():
    result = _parse_titles_bulk("Severance; Stranger Things")
    assert result == ["Severance", "Stranger Things"]


def test_mixed_separators_on_one_line_just_commas():
    result = _parse_titles_bulk("Дюна, Дюна 2, Дюна 3")
    assert result == ["Дюна", "Дюна 2", "Дюна 3"]


# ---------- Edge cases ----------

def test_empty_input():
    assert _parse_titles_bulk("") == []


def test_only_separators():
    assert _parse_titles_bulk("\n\n,,;;") == []


def test_unicode_dash_bullet():
    # En-dash
    result = _parse_titles_bulk("– Severance\n– Dark")
    assert result == ["Severance", "Dark"]
