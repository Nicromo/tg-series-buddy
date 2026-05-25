"""Генератор постера-картинки «Наша неделя» для шеринга.

Делаем 1080×1350 (вертикальный, сторис-формат).

КРИТИЧНО про шрифты: на Render (Linux slim) нет Windows-шрифтов,
а ImageFont.load_default() не поддерживает кириллицу. Поэтому:
1. В Dockerfile установлен пакет fonts-dejavu-core
2. Здесь ищем DejaVuSans именно по Linux-путям

Эмодзи в тексте НЕ используем — обычный TTF их не рендерит как
цветные (Pillow без embedded_color не умеет цветные эмодзи).
Вместо этого — текстовые подписи и геометрические бэйджи.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Шрифты в порядке предпочтения. На Render (после Dockerfile с
# fonts-dejavu-core) сработает первый блок.
_FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]
_FONT_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    candidates = _FONT_PATHS_BOLD if bold else _FONT_PATHS_REGULAR
    for p in candidates:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    logger.warning("No TTF font found — using bitmap default (cyrillic will break)")
    return ImageFont.load_default()


async def _fetch_poster_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
    except Exception as e:
        logger.warning("poster fetch %s failed: %s", url, e)
    return None


_STATUS_TEXT = {
    "▶️ Смотрим": "СМОТРИМ",
    "👀 Хотим": "ХОТИМ",
    "✅ Досмотрено": "ДОСМОТРЕЛИ",
    "🔁 Пересмотр": "ПЕРЕСМОТР",
}
_STATUS_COLOR = {
    "СМОТРИМ":    "#ff7eb3",
    "ХОТИМ":      "#ffd86b",
    "ДОСМОТРЕЛИ": "#9be36b",
    "ПЕРЕСМОТР":  "#7cc6ff",
}


def _strip_emoji(status_label: str) -> tuple[str, str]:
    """«▶️ Смотрим» → («СМОТРИМ», цвет)."""
    label = _STATUS_TEXT.get(status_label, status_label)
    color = _STATUS_COLOR.get(label, "#ffffff")
    return label, color


async def build_week_wallpaper(
    items: list,
    *,
    bot_username: str = "dvoye_na_divane_bot",
    header: str = "Наша неделя",
) -> Optional[bytes]:
    """Строит PNG 1080×1350. items = [(status_label, Series), ...]."""
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        logger.warning("Pillow not installed")
        return None

    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), "#120822")
    draw = ImageDraw.Draw(img)

    # Тёмный градиент: фиолет → индиго
    for y in range(H):
        t = y / H
        r = int(18 + (45 - 18) * t)
        g = int(8 + (15 - 8) * t)
        b = int(34 + (85 - 34) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Шапка: бренд (без эмодзи в тексте — оно не рендерится)
    draw.text((W // 2, 80), "DIVANNYE KRITIKI", font=_font(36, bold=True), anchor="mm", fill="#ffd86b")
    # Декоративная линия под брендом
    draw.line([(W // 2 - 220, 110), (W // 2 + 220, 110)], fill="#ffd86b", width=2)
    # Большой заголовок
    draw.text((W // 2, 175), header.upper(), font=_font(80, bold=True), anchor="mm", fill="#ffffff")

    if not items:
        draw.text((W // 2, H // 2), "Список пуст", font=_font(48), anchor="mm", fill="#aaa")
        draw.text((W // 2, H - 60), f"@{bot_username}", font=_font(26), anchor="mm", fill="#888")
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    sub = f"{len(items)} тайтлов — общий список с партнёром"
    draw.text((W // 2, 235), sub, font=_font(28), anchor="mm", fill="#c8c0e0")

    # Скачиваем постеры параллельно
    import asyncio
    poster_bytes = await asyncio.gather(*(
        _fetch_poster_bytes(s.poster_url) if s.poster_url else _no_poster()
        for _, s in items[:5]
    ))

    # Главный постер
    main_pb = poster_bytes[0] if poster_bytes else None
    main_y_top = 300
    main_h = 0
    if main_pb:
        try:
            main_im = Image.open(io.BytesIO(main_pb)).convert("RGB")
            target_w = 360
            ratio = target_w / main_im.width
            mw = target_w
            mh = int(main_im.height * ratio)
            main_im = main_im.resize((mw, mh))
            # Тень
            shadow = Image.new("RGBA", (mw + 50, mh + 50), (0, 0, 0, 0))
            sd = ImageDraw.Draw(shadow)
            sd.rounded_rectangle([25, 25, mw + 25, mh + 25], 20, fill=(0, 0, 0, 180))
            shadow = shadow.filter(ImageFilter.GaussianBlur(12))
            img.paste(shadow, ((W - mw - 50) // 2, main_y_top - 25), shadow)
            mask = Image.new("L", (mw, mh), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, mw, mh], 20, fill=255)
            img.paste(main_im, ((W - mw) // 2, main_y_top), mask)

            # Бейдж со статусом (цветная плашка вместо эмодзи в тексте)
            status_label, status_color = _strip_emoji(items[0][0])
            badge_text = status_label
            f_badge = _font(26, bold=True)
            tbox = draw.textbbox((0, 0), badge_text, font=f_badge)
            tw = tbox[2] - tbox[0]
            th = tbox[3] - tbox[1]
            pad_x, pad_y = 24, 12
            bw = tw + pad_x * 2
            bh = th + pad_y * 2
            bx = (W - bw) // 2
            by = main_y_top + mh + 25
            draw.rounded_rectangle([bx, by, bx + bw, by + bh], 18, fill=status_color)
            draw.text((W // 2, by + bh // 2), badge_text, font=f_badge, anchor="mm", fill="#1a0e30")

            # Название под бейджем
            title = items[0][1].title_ru or ""
            if len(title) > 28:
                title = title[:28] + "…"
            draw.text((W // 2, by + bh + 30), title, font=_font(32, bold=True), anchor="mm", fill="#ffffff")
            main_h = mh + bh + 100
        except Exception as e:
            logger.warning("main poster paste failed: %s", e)

    # 4 мелких в сетке 2×2 — постер + цветная плашка статуса + название
    grid_top = main_y_top + (main_h if main_h else 460) + 20
    cell_w = 220
    cell_h = 330
    gap_x = 40
    gap_y = 100
    total_w = cell_w * 2 + gap_x
    start_x = (W - total_w) // 2
    for i in range(1, min(5, len(items))):
        col = (i - 1) % 2
        row = (i - 1) // 2
        x = start_x + col * (cell_w + gap_x)
        y = grid_top + row * (cell_h + gap_y)
        pb = poster_bytes[i] if i < len(poster_bytes) else None
        if pb:
            try:
                p_im = Image.open(io.BytesIO(pb)).convert("RGB")
                pr = cell_w / p_im.width
                pw = cell_w
                ph = int(p_im.height * pr)
                if ph > cell_h:
                    pr = cell_h / p_im.height
                    ph = cell_h
                    pw = int(p_im.width * pr)
                small = p_im.resize((pw, ph))
                mask = Image.new("L", (pw, ph), 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw, ph], 14, fill=255)
                img.paste(small, (x + (cell_w - pw) // 2, y), mask)
            except Exception as e:
                logger.warning("small poster paste failed: %s", e)

        status_label, status_color = _strip_emoji(items[i][0])
        # Плашка статуса
        f_st = _font(18, bold=True)
        tbox = draw.textbbox((0, 0), status_label, font=f_st)
        tw = tbox[2] - tbox[0]
        th = tbox[3] - tbox[1]
        pad_x, pad_y = 14, 7
        bw = tw + pad_x * 2
        bh = th + pad_y * 2
        bx = x + (cell_w - bw) // 2
        by = y + cell_h + 14
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], 12, fill=status_color)
        draw.text((x + cell_w // 2, by + bh // 2), status_label, font=f_st, anchor="mm", fill="#1a0e30")

        title = items[i][1].title_ru or ""
        if len(title) > 20:
            title = title[:20] + "…"
        draw.text((x + cell_w // 2, by + bh + 24), title, font=_font(20, bold=True), anchor="mm", fill="#ffffff")

    # Футер
    draw.line([(W // 2 - 220, H - 80), (W // 2 + 220, H - 80)], fill="#ffd86b", width=1)
    draw.text((W // 2, H - 50), f"@{bot_username}", font=_font(26, bold=True), anchor="mm", fill="#ffd86b")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def _no_poster() -> Optional[bytes]:
    return None
