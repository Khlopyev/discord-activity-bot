"""Генератор баннера профиля бота (680×240 — рекомендованный размер Discord).

    python tools/make_banner.py assets

Композиция учитывает, что Discord накладывает круглую аватарку на нижний левый
угол баннера: столбики слева низкие, вся визуальная тяжесть смещена вправо,
чтобы аватарка ничего не перекрывала.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))

from brand import (  # noqa: E402
    ACCENT,
    ACCENT_SOFT,
    PUPIL,
    SCLERA,
    SCLERA_SHADE,
    backdrop,
    circle_mask,
    eye_mask,
    fade,
    horizontal_gradient,
    vertical_gradient,
)

WIDTH, HEIGHT = 680, 240
SS = 3
W, H = WIDTH * SS, HEIGHT * SS
CANVAS = (W, H)

# Высоты столбиков: тренд вверх слева направо — левый край остаётся спокойным
# под аватаркой, акцент уходит вправо.
BAR_HEIGHTS = (
    0.20, 0.29, 0.17, 0.34, 0.26, 0.41, 0.31, 0.49, 0.36, 0.54, 0.45, 0.61, 0.43,
    0.57, 0.67, 0.51, 0.71, 0.59, 0.79, 0.65, 0.85, 0.73, 0.91, 0.69, 0.96, 0.81,
)


def _background() -> Image.Image:
    return backdrop(
        CANVAS,
        [
            ((-W // 5, -H, W // 3, H), ACCENT),
            ((W - W // 3, H // 4, W + W // 6, H * 2), ACCENT_SOFT),
        ],
        blur=W // 12,
        strength=0.34,
    )


def _bars_mask(height_factor: float) -> Image.Image:
    mask = Image.new("L", CANVAS, 0)
    draw = ImageDraw.Draw(mask)
    slot = W / len(BAR_HEIGHTS)
    bar_w = slot * 0.52
    max_height = H * height_factor

    for index, factor in enumerate(BAR_HEIGHTS):
        centre = slot * (index + 0.5)
        x0 = centre - bar_w / 2
        top = H - max_height * factor
        draw.rounded_rectangle(
            (x0, top, x0 + bar_w, H + bar_w), radius=bar_w / 2, fill=255
        )
    return mask


def _eye_watermark(image: Image.Image) -> None:
    """Глаз-водяной знак справа — перекликается с аватаркой.

    Сидит выше линии столбиков: пересечение с ними читалось бы как грязь.
    """
    cx, cy = W * 0.80, H * 0.36
    half_w, half_h = W * 0.165, H * 0.255
    iris_r = half_h * 0.72

    eye = eye_mask(CANVAS, cx, cy, half_w, half_h)
    image.paste(vertical_gradient(CANVAS, SCLERA, SCLERA_SHADE), (0, 0), fade(eye, 0.20))

    iris = ImageChops.darker(circle_mask(CANVAS, cx, cy, iris_r), eye)
    image.paste(vertical_gradient(CANVAS, ACCENT, ACCENT_SOFT), (0, 0), fade(iris, 0.60))
    image.paste(
        Image.new("RGB", CANVAS, PUPIL),
        (0, 0),
        fade(ImageChops.darker(circle_mask(CANVAS, cx, cy, iris_r * 0.52), eye), 0.50),
    )


def render(with_eye: bool) -> Image.Image:
    image = _background()
    # Со знаком столбики ниже, чтобы освободить ему место по вертикали.
    height_factor = 0.44 if with_eye else 0.66
    if with_eye:
        _eye_watermark(image)
    image.paste(
        horizontal_gradient(CANVAS, ACCENT, ACCENT_SOFT),
        (0, 0),
        fade(_bars_mask(height_factor), 0.62),
    )
    return image.resize((WIDTH, HEIGHT), Image.LANCZOS)


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "assets")
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, with_eye in (("banner", True), ("banner-plain", False)):
        path = out_dir / f"{name}.png"
        render(with_eye).save(path, format="PNG", optimize=True)
        print("saved", path)


if __name__ == "__main__":
    main()
