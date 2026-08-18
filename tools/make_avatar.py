"""Генератор аватарки бота.

Тема: «Большой Брат» — глаз наблюдения. Палитра общая с карточкой профиля
и баннером (см. `tools/brand.py`).

    python tools/make_avatar.py assets

Discord обрезает аватар в круг, поэтому фон залит до самых краёв квадрата.
Вариант `--bars` вырезает из радужки столбики статистики.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

# Скрипт запускается и напрямую, и импортируется превью-скриптами, поэтому
# каталог tools/ добавляется в путь явно.
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
    vertical_gradient,
)

SIZE = 512
SS = 4  # кратность отрисовки для сглаживания
S = SIZE * SS
CANVAS = (S, S)

CX = CY = S // 2
EYE_HALF_W = S * 0.375
EYE_HALF_H = S * 0.205
IRIS_R = S * 0.166


def _background() -> Image.Image:
    return backdrop(
        CANVAS,
        [
            ((-S // 3, -S // 3, S // 2, S // 2), ACCENT),
            ((S // 2, S // 2, S + S // 3, S + S // 3), ACCENT_SOFT),
        ],
        blur=S // 8,
    )


def _bars_mask() -> Image.Image:
    """Три столбика активности — та же метафора, что и график на карточке."""
    mask = Image.new("L", CANVAS, 0)
    draw = ImageDraw.Draw(mask)
    bar_w = int(IRIS_R * 0.30)
    gap = int(IRIS_R * 0.17)
    baseline = CY + int(IRIS_R * 0.56)
    left = CX - (3 * bar_w + 2 * gap) // 2
    for index, factor in enumerate((0.62, 1.06, 0.82)):
        x0 = left + index * (bar_w + gap)
        draw.rounded_rectangle(
            (x0, baseline - int(IRIS_R * factor), x0 + bar_w, baseline),
            radius=bar_w // 2,
            fill=255,
        )
    return mask


def render(with_bars: bool) -> Image.Image:
    image = _background()
    sclera = vertical_gradient(CANVAS, SCLERA, SCLERA_SHADE)
    eye = eye_mask(CANVAS, CX, CY, EYE_HALF_W, EYE_HALF_H)
    image.paste(sclera, (0, 0), eye)

    iris = vertical_gradient(CANVAS, ACCENT, ACCENT_SOFT)
    # Радужка не должна вылезать за контур глаза.
    image.paste(iris, (0, 0), ImageChops.darker(circle_mask(CANVAS, CX, CY, IRIS_R), eye))

    if with_bars:
        # Столбики вырезаны из радужки — сквозь них просвечивает белок.
        image.paste(sclera, (0, 0), ImageChops.darker(_bars_mask(), eye))
    else:
        image.paste(Image.new("RGB", CANVAS, PUPIL), (0, 0),
                    circle_mask(CANVAS, CX, CY, IRIS_R * 0.52))
        image.paste(
            Image.new("RGB", CANVAS, SCLERA),
            (0, 0),
            circle_mask(CANVAS, CX + IRIS_R * 0.30, CY - IRIS_R * 0.30, IRIS_R * 0.17),
        )

    return image.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "assets")
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, with_bars in (("avatar", False), ("avatar-bars", True)):
        path = out_dir / f"{name}.png"
        render(with_bars).save(path, format="PNG", optimize=True)
        print("saved", path)


if __name__ == "__main__":
    main()
