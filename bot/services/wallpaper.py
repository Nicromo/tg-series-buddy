"""Генератор постера-картинки «Наша неделя» для шеринга.

PNG 1080×1350 (вертикальный, сторис-формат).

КРИТИЧНО про шрифты: на Render (Linux slim) нет Windows-шрифтов.
В Dockerfile установлен fonts-dejavu-core.

Дизайн: главный постер сверху по центру, остальные коллажем
с лёгкими наклонами. Декоративные точки на фоне для оживления.
"""

from __future__ import annotations

import io
import logging
import os
import random
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


_FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
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
    "▶️ Смотрим":    "СМОТРИМ",
    "👀 Хотим":       "ХОТИМ",
    "✅ Досмотрено":  "ДОСМОТРЕЛИ",
    "🔁 Пересмотр":   "ПЕРЕСМОТР",
}
_STATUS_COLOR = {
    "СМОТРИМ":     "#ff7eb3",
    "ХОТИМ":       "#ffd86b",
    "ДОСМОТРЕЛИ":  "#9be36b",
    "ПЕРЕСМОТР":   "#7cc6ff",
}


def _strip_emoji(status_label: str) -> tuple[str, str]:
    label = _STATUS_TEXT.get(status_label, status_label)
    color = _STATUS_COLOR.get(label, "#ffffff")
    return label, color


def _decline_titles(n: int) -> str:
    n_abs = abs(n) % 100
    if 10 <= n_abs <= 20:
        return "тайтлов"
    last = n_abs % 10
    if last == 1:
        return "тайтл"
    if 2 <= last <= 4:
        return "тайтла"
    return "тайтлов"


# ============== Layout ==============

def _layout_for(count: int, content_top: int, content_bottom: int) -> list[dict]:
    """Возвращает список dict для каждого постера:
    {'w': ширина, 'h': высота, 'cx': центр X, 'cy': центр Y, 'rot': угол°, 'main': bool}.

    Дизайн коллажа:
    - 1: один крупный по центру
    - 2: два бок-о-бок с наклонами в противоположные стороны
    - 3: главный сверху, 2 мелких внизу
    - 4: главный сверху, 3 мелких в ряд внизу
    - 5: главный сверху, 4 мелких 2×2
    """
    W = 1080
    cx_center = W // 2
    if count == 1:
        return [{"w": 480, "h": 720, "cx": cx_center, "cy": content_top + 360, "rot": 0, "main": True}]
    if count == 2:
        w = 380
        h = 570
        y = content_top + h // 2 + 30
        return [
            {"w": w, "h": h, "cx": cx_center - 200, "cy": y, "rot": -4, "main": True},
            {"w": w, "h": h, "cx": cx_center + 200, "cy": y, "rot": +4, "main": False},
        ]
    if count == 3:
        layout = []
        # Главный сверху
        mw, mh = 380, 570
        my = content_top + mh // 2 + 20
        layout.append({"w": mw, "h": mh, "cx": cx_center, "cy": my, "rot": 0, "main": True})
        # 2 ниже бок-о-бок, наклонены
        sw, sh = 280, 420
        sy = content_bottom - sh // 2 - 100
        layout.append({"w": sw, "h": sh, "cx": cx_center - 170, "cy": sy, "rot": -5, "main": False})
        layout.append({"w": sw, "h": sh, "cx": cx_center + 170, "cy": sy, "rot": +5, "main": False})
        return layout
    if count == 4:
        layout = []
        mw, mh = 360, 540
        my = content_top + mh // 2 + 20
        layout.append({"w": mw, "h": mh, "cx": cx_center, "cy": my, "rot": 0, "main": True})
        # 3 мелких в ряд
        sw, sh = 240, 360
        sy = content_bottom - sh // 2 - 100
        gap = 30
        total = sw * 3 + gap * 2
        start_x = (W - total) // 2 + sw // 2
        for i, rot in enumerate((-4, +3, -3)):
            layout.append({
                "w": sw, "h": sh,
                "cx": start_x + i * (sw + gap),
                "cy": sy, "rot": rot, "main": False,
            })
        return layout
    # 5+: главный + 4 в 2×2
    layout = []
    mw, mh = 360, 540
    my = content_top + mh // 2 + 20
    layout.append({"w": mw, "h": mh, "cx": cx_center, "cy": my, "rot": 0, "main": True})
    sw, sh = 240, 360
    rotations = [(-5, +4), (+3, -4)]
    grid_top_y = my + mh // 2 + 90
    gap_x = 50
    for row, row_rots in enumerate(rotations):
        for col, rot in enumerate(row_rots):
            cx = cx_center + (col * 2 - 1) * (sw + gap_x) // 2
            cy = grid_top_y + row * (sh + 110) + sh // 2
            layout.append({"w": sw, "h": sh, "cx": cx, "cy": cy, "rot": rot, "main": False})
    return layout


def _paste_rotated_poster(canvas, poster_img, w: int, h: int, cx: int, cy: int, rot: float):
    """Вставляет постер с округлёнными углами и наклоном в canvas (center at cx, cy)."""
    from PIL import Image, ImageDraw, ImageFilter
    # Ресайз поста с сохранением пропорций под bounding box
    pr = min(w / poster_img.width, h / poster_img.height)
    pw, ph = max(1, int(poster_img.width * pr)), max(1, int(poster_img.height * pr))
    resized = poster_img.resize((pw, ph), Image.LANCZOS)
    # Скруглённые углы через mask
    rgba = resized.convert("RGBA")
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw, ph], 18, fill=255)
    rgba.putalpha(mask)
    # Тень — отдельный rect под постером
    shadow_layer = Image.new("RGBA", (pw + 60, ph + 60), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    sd.rounded_rectangle([30, 30, pw + 30, ph + 30], 18, fill=(0, 0, 0, 180))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(15))
    # Поворачиваем постер и тень
    rotated_poster = rgba.rotate(rot, expand=True, resample=Image.BICUBIC)
    rotated_shadow = shadow_layer.rotate(rot, expand=True, resample=Image.BICUBIC)
    # Координаты paste с учётом expand
    sw, sh = rotated_shadow.size
    canvas.paste(rotated_shadow, (cx - sw // 2, cy - sh // 2 + 8), rotated_shadow)
    rw, rh = rotated_poster.size
    canvas.paste(rotated_poster, (cx - rw // 2, cy - rh // 2), rotated_poster)
    return pw, ph


def _draw_status_badge(draw, text: str, color: str, cx: int, cy: int):
    """Рисует цветной бейдж со статусом, центр в (cx, cy)."""
    f_badge = _font(22, bold=True)
    tbox = draw.textbbox((0, 0), text, font=f_badge)
    tw = tbox[2] - tbox[0]
    th = tbox[3] - tbox[1]
    pad_x, pad_y = 16, 8
    bw = tw + pad_x * 2
    bh = th + pad_y * 2
    bx = cx - bw // 2
    by = cy - bh // 2
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], 14, fill=color)
    draw.text((cx, cy), text, font=f_badge, anchor="mm", fill="#1a0e30")
    return bh


def _draw_bg_decor(draw, W: int, H: int):
    """Декоративные точки/звёздочки на фоне для оживления."""
    random.seed(42)  # стабильно — каждый wallpaper выглядит одинаково
    colors = ["#3a2865", "#4d3580", "#6b4ab2", "#ffd86b22"]
    for _ in range(120):
        x = random.randint(0, W)
        y = random.randint(0, H)
        r = random.choice([1, 1, 2, 2, 3])
        color = random.choice(colors)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


# ============== Главная функция ==============

async def build_week_wallpaper(
    items: list,
    *,
    bot_username: str = "dvoye_na_divane_bot",
    header: str = "Наша неделя",
) -> Optional[bytes]:
    """Строит PNG 1080×1350. items = [(status_label, Series), ...]."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed")
        return None

    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), "#120822")
    draw = ImageDraw.Draw(img)

    # Градиент: фиолет → индиго
    for y in range(H):
        t = y / H
        r = int(18 + (45 - 18) * t)
        g = int(8 + (15 - 8) * t)
        b = int(34 + (85 - 34) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Декоративный «шум» — точки
    _draw_bg_decor(draw, W, H)

    # Header
    draw.text((W // 2, 70), "DIVANNYE KRITIKI", font=_font(32, bold=True), anchor="mm", fill="#ffd86b")
    draw.line([(W // 2 - 200, 100), (W // 2 + 200, 100)], fill="#ffd86b", width=2)
    draw.text((W // 2, 165), header.upper(), font=_font(72, bold=True), anchor="mm", fill="#ffffff")

    if not items:
        draw.text((W // 2, H // 2), "Список пуст", font=_font(48), anchor="mm", fill="#aaa")
        draw.text((W // 2, H - 60), f"@{bot_username}", font=_font(26), anchor="mm", fill="#888")
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    # Скачиваем постеры параллельно
    import asyncio
    poster_bytes = await asyncio.gather(*(
        _fetch_poster_bytes(s.poster_url) if s.poster_url else _no_poster()
        for _, s in items[:5]
    ))
    # Фильтруем неудачи
    valid_items: list = []
    valid_bytes: list = []
    for (status, s), pb in zip(items, poster_bytes):
        if pb:
            valid_items.append((status, s))
            valid_bytes.append(pb)
    real_count = len(valid_items)

    sub = f"{real_count} {_decline_titles(real_count)} — общий список с партнёром"
    draw.text((W // 2, 220), sub, font=_font(26), anchor="mm", fill="#c8c0e0")

    # Адаптивный layout
    content_top = 270
    content_bottom = H - 130  # оставляем место для футера и подписей
    layout = _layout_for(real_count, content_top, content_bottom)

    # Рендерим постеры + бейджи
    for (status_label, series), pb, geo in zip(valid_items, valid_bytes, layout):
        try:
            p_im = Image.open(io.BytesIO(pb)).convert("RGB")
        except Exception as e:
            logger.warning("Image open failed: %s", e)
            continue
        actual_w, actual_h = _paste_rotated_poster(
            img, p_im, geo["w"], geo["h"], geo["cx"], geo["cy"], geo["rot"],
        )
        # Бейдж и название — НЕ поворачиваем, чтобы читалось
        st_label, st_color = _strip_emoji(status_label)
        # Бейдж под постером (учитывая поворот — поднимаем чуть больше)
        badge_y = geo["cy"] + geo["h"] // 2 + 30
        bh = _draw_status_badge(draw, st_label, st_color, geo["cx"], badge_y)
        # Название под бейджем
        title = series.title_ru or ""
        max_len = 30 if geo["main"] else 18
        if len(title) > max_len:
            title = title[:max_len] + "…"
        title_size = 28 if geo["main"] else 20
        draw.text(
            (geo["cx"], badge_y + bh // 2 + 22),
            title,
            font=_font(title_size, bold=True),
            anchor="mm",
            fill="#ffffff",
        )

    # Footer
    draw.line([(W // 2 - 220, H - 75), (W // 2 + 220, H - 75)], fill="#ffd86b", width=1)
    draw.text((W // 2, H - 45), f"@{bot_username}", font=_font(24, bold=True), anchor="mm", fill="#ffd86b")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def _no_poster() -> Optional[bytes]:
    return None
