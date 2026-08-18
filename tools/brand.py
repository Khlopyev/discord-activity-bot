"""Общая палитра и графические примитивы для айдентики бота.

Используется генераторами аватарки и баннера, чтобы они не разъезжались.
Палитра совпадает с карточкой профиля (`bot/card.py`).

У Pillow нет сглаживания примитивов, поэтому всё рисуется с кратным
увеличением и уменьшается в конце — см. `SS` в вызывающих скриптах.
"""

from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter

Colour = tuple[int, int, int]
Size = tuple[int, int]

BG_TOP: Colour = (32, 34, 44)
BG_BOTTOM: Colour = (14, 15, 19)
ACCENT: Colour = (88, 101, 242)
ACCENT_SOFT: Colour = (139, 92, 246)
SCLERA: Colour = (226, 232, 246)
SCLERA_SHADE: Colour = (168, 180, 214)
PUPIL: Colour = (18, 19, 26)


def vertical_gradient(size: Size, top: Colour, bottom: Colour) -> Image.Image:
    width, height = size
    strip = Image.new("RGB", (1, height))
    for y in range(height):
        ratio = y / max(1, height - 1)
        strip.putpixel((0, y), _lerp(top, bottom, ratio))
    return strip.resize(size)


def horizontal_gradient(size: Size, left: Colour, right: Colour) -> Image.Image:
    width, height = size
    strip = Image.new("RGB", (width, 1))
    for x in range(width):
        ratio = x / max(1, width - 1)
        strip.putpixel((x, 0), _lerp(left, right, ratio))
    return strip.resize(size)


def backdrop(size: Size, blobs: list[tuple[tuple[float, float, float, float], Colour]],
             *, blur: int, strength: float = 0.30) -> Image.Image:
    """Тёмный градиент с мягкими цветными пятнами — общий фон айдентики."""
    base = vertical_gradient(size, BG_TOP, BG_BOTTOM)
    glow = Image.new("RGB", size, BG_BOTTOM)
    draw = ImageDraw.Draw(glow)
    for box, colour in blobs:
        draw.ellipse(box, fill=colour)
    return Image.blend(base, glow.filter(ImageFilter.GaussianBlur(blur)), strength)


def circle_mask(size: Size, cx: float, cy: float, radius: float) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius), fill=255
    )
    return mask


def eye_mask(size: Size, cx: float, cy: float, half_w: float, half_h: float) -> Image.Image:
    """Миндалевидная форма как пересечение двух больших окружностей.

    Смещение и радиус подобраны так, чтобы дуги сошлись ровно в углах глаза.
    """
    offset = (half_w**2 - half_h**2) / (2 * half_h)
    radius = offset + half_h

    upper = Image.new("L", size, 0)
    ImageDraw.Draw(upper).ellipse(
        (cx - radius, cy + offset - radius, cx + radius, cy + offset + radius), fill=255
    )
    lower = Image.new("L", size, 0)
    ImageDraw.Draw(lower).ellipse(
        (cx - radius, cy - offset - radius, cx + radius, cy - offset + radius), fill=255
    )
    return ImageChops.darker(upper, lower)


def fade(mask: Image.Image, alpha: float) -> Image.Image:
    """Приглушить маску — для полупрозрачных наложений."""
    factor = max(0.0, min(1.0, alpha))
    return mask.point(lambda value: int(value * factor))


def _lerp(a: Colour, b: Colour, ratio: float) -> Colour:
    return tuple(int(x + (y - x) * ratio) for x, y in zip(a, b))
