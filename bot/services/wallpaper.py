"""Генератор постера-картинки «Наша неделя» для шеринга.

Принимает на вход список активных сериалов (max 5), скачивает с КП
постеры и собирает PNG 1080×1350 (вертикальный формат для сторис).
"""

from __future__ import annotations

import io
import logging
import os
import random
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _font(size: int, bold: bool = False):
    """Лучший доступный шрифт с поддержкой кириллицы."""
    from PIL import ImageFont
    candidates = (
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans.ttf",
    )
    for p in candidates:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
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


async def build_week_wallpaper(
    items: list,  # list of (status_label, Series) — где status_label вроде "▶️ Смотрим"
    *,
    bot_username: str = "dvoye_na_divane_bot",
    header: str = "Наша неделя",
) -> Optional[bytes]:
    """Строит PNG 1080×1350. Возвращает bytes или None при ошибке."""
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        logger.warning("Pillow not installed — /wallpaper disabled")
        return None

    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), "#0f0820")
    draw = ImageDraw.Draw(img)

    # Градиент фона: тёмно-фиолетовый → тёмно-синий
    for y in range(H):
        t = y / H
        r = int(15 + (60 - 15) * t)
        g = int(8 + (20 - 8) * t)
        b = int(32 + (95 - 32) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Заголовки
    draw.text((W // 2, 80), "🛋 Диванные критики", font=_font(48, bold=True), anchor="mm", fill="#ffd86b")
    draw.text((W // 2, 155), header, font=_font(64, bold=True), anchor="mm", fill="#fff")
    sub = f"{len(items)} тайтлов · общий список с партнёром" if items else ""
    if sub:
        draw.text((W // 2, 215), sub, font=_font(28), anchor="mm", fill="#bba")

    if not items:
        draw.text((W // 2, H // 2), "Список пуст 🤷", font=_font(64), anchor="mm", fill="#fff")
        draw.text((W // 2, H - 60), f"@{bot_username}", font=_font(28), anchor="mm", fill="#888")
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    # Скачиваем постеры параллельно
    import asyncio
    poster_bytes = await asyncio.gather(*(
        _fetch_poster_bytes(s.poster_url) if s.poster_url else _no_poster()
        for _, s in items[:5]
    ))

    # Главный постер сверху по центру, ниже до 4 мелких
    main_idx = 0
    main_pb = poster_bytes[0] if poster_bytes else None
    if main_pb:
        try:
            main_im = Image.open(io.BytesIO(main_pb)).convert("RGB")
            ratio = 340 / main_im.width
            mw = int(main_im.width * ratio)
            mh = int(main_im.height * ratio)
            main_im = main_im.resize((mw, mh))
            # Тень
            shadow = Image.new("RGBA", (mw + 40, mh + 40), (0, 0, 0, 0))
            sd = ImageDraw.Draw(shadow)
            sd.rounded_rectangle([20, 20, mw + 20, mh + 20], 18, fill=(0, 0, 0, 160))
            shadow = shadow.filter(ImageFilter.GaussianBlur(10))
            img.paste(shadow, ((W - mw - 40) // 2, 260 - 20), shadow)
            mask = Image.new("L", (mw, mh), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, mw, mh], 18, fill=255)
            img.paste(main_im, ((W - mw) // 2, 260), mask)
            # Подпись главного
            status, s = items[0]
            draw.text(
                (W // 2, 260 + mh + 30),
                f"{status} · {s.title_ru}",
                font=_font(30, bold=True), anchor="mm", fill="#fff",
            )
        except Exception as e:
            logger.warning("main poster paste failed: %s", e)
            mh = 0
    else:
        mh = 0

    # 4 мелких в сетке 2×2
    grid_top = 260 + (mh if mh else 360) + 100
    cell_w = 240
    cell_h = 360
    gap = 30
    total_w = cell_w * 2 + gap
    start_x = (W - total_w) // 2
    for i in range(1, min(5, len(items))):
        col = (i - 1) % 2
        row = (i - 1) // 2
        x = start_x + col * (cell_w + gap)
        y = grid_top + row * (cell_h + 80)
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
        status, s = items[i]
        # Подпись
        draw.text((x + cell_w // 2, y + cell_h + 20), status, font=_font(22, bold=True), anchor="mm", fill="#ffd86b")
        title = (s.title_ru or "")[:22]
        if len((s.title_ru or "")) > 22:
            title += "…"
        draw.text((x + cell_w // 2, y + cell_h + 50), title, font=_font(20), anchor="mm", fill="#dde")

    # Футер
    draw.text((W // 2, H - 50), f"@{bot_username}", font=_font(26), anchor="mm", fill="#888")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def _no_poster() -> Optional[bytes]:
    return None
