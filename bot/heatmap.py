"""Рендер тепловой карты активности (раздел 3.2 брифа).

Сетка 7×24: строки — дни недели с понедельника, столбцы — часы суток в
тайм-зоне агрегации. Насыщенность ячейки — доля от самого активного слота.

Как и карточка профиля, рисуется блокирующим Pillow и вызывается через
`asyncio.to_thread`.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw

from .card import (
    ACCENT,
    ACCENT_SOFT,
    BG_BOTTOM,
    BG_TOP,
    PANEL,
    PANEL_EDGE,
    TEXT,
    TEXT_DIM,
    TEXT_MUTED,
    fonts,
)

WIDTH, HEIGHT = 920, 400
PAD = 32
GRID_TOP = 110
GRID_LEFT = 92
CELL_GAP = 3
ROWS, COLS = 7, 24

# Строки идут с понедельника — так читают люди, хотя в БД неделя начинается
# с воскресенья (нотация strftime('%w')).
WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
DISPLAY_TO_SQLITE = (1, 2, 3, 4, 5, 6, 0)


@dataclass
class HeatmapData:
    title: str
    subtitle: str
    # (день недели в нотации SQLite, час) -> вес активности
    slots: dict[tuple[int, int], float]
    footer: str


def render_heatmap(data: HeatmapData) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG_BOTTOM)
    _paint_background(image)
    draw = ImageDraw.Draw(image)

    draw.text((PAD, PAD - 4), data.title, font=fonts.get(26, bold=True), fill=TEXT)
    draw.text((PAD, PAD + 30), data.subtitle, font=fonts.get(15), fill=TEXT_MUTED)

    cell_w = (WIDTH - GRID_LEFT - PAD - (COLS - 1) * CELL_GAP) / COLS
    cell_h = (HEIGHT - GRID_TOP - 78 - (ROWS - 1) * CELL_GAP) / ROWS
    peak = max(data.slots.values(), default=0.0)

    _draw_hour_labels(draw, cell_w)
    _draw_cells(draw, data, cell_w, cell_h, peak)
    _draw_weekday_labels(draw, cell_h)
    _draw_legend(draw, cell_h)

    draw.text((PAD, HEIGHT - 26), data.footer, font=fonts.get(13), fill=TEXT_DIM)

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _paint_background(image: Image.Image) -> None:
    strip = Image.new("RGB", (1, HEIGHT))
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        strip.putpixel((0, y), _mix(BG_TOP, BG_BOTTOM, ratio))
    image.paste(strip.resize((WIDTH, HEIGHT)), (0, 0))


def _draw_hour_labels(draw: ImageDraw.ImageDraw, cell_w: float) -> None:
    font = fonts.get(12)
    for hour in range(COLS):
        if hour % 3:
            continue
        x = GRID_LEFT + hour * (cell_w + CELL_GAP)
        draw.text((x, GRID_TOP - 19), f"{hour:02d}", font=font, fill=TEXT_DIM)


def _draw_cells(
    draw: ImageDraw.ImageDraw, data: HeatmapData, cell_w: float, cell_h: float, peak: float
) -> None:
    for row, weekday in enumerate(DISPLAY_TO_SQLITE):
        y0 = GRID_TOP + row * (cell_h + CELL_GAP)
        for hour in range(COLS):
            x0 = GRID_LEFT + hour * (cell_w + CELL_GAP)
            value = data.slots.get((weekday, hour), 0.0)
            if peak > 0 and value > 0:
                # Корень сжимает разброс: иначе один пиковый слот гасит остальные.
                intensity = (value / peak) ** 0.5
                colour = _mix(PANEL, _mix(ACCENT, ACCENT_SOFT, hour / (COLS - 1)), intensity)
            else:
                colour = PANEL
            draw.rounded_rectangle(
                (x0, y0, x0 + cell_w, y0 + cell_h), radius=3, fill=colour
            )


def _draw_weekday_labels(draw: ImageDraw.ImageDraw, cell_h: float) -> None:
    font = fonts.get(13, bold=True)
    for row, label in enumerate(WEEKDAY_LABELS):
        y = GRID_TOP + row * (cell_h + CELL_GAP) + cell_h / 2 - 8
        draw.text((PAD, y), label, font=font, fill=TEXT_MUTED)


def _draw_legend(draw: ImageDraw.ImageDraw, cell_h: float) -> None:
    y = GRID_TOP + ROWS * (cell_h + CELL_GAP) + 18
    font = fonts.get(12)
    draw.text((GRID_LEFT, y + 2), "реже", font=font, fill=TEXT_DIM)

    x = GRID_LEFT + 44
    steps = 6
    for step in range(steps):
        colour = _mix(PANEL, ACCENT_SOFT, (step + 1) / steps)
        draw.rounded_rectangle((x, y, x + 22, y + 16), radius=3, fill=colour)
        x += 26
    draw.text((x + 6, y + 2), "чаще", font=font, fill=TEXT_DIM)

    draw.line(
        (GRID_LEFT, y - 10, WIDTH - PAD, y - 10), fill=PANEL_EDGE, width=1
    )


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    ratio = max(0.0, min(1.0, ratio))
    return tuple(int(x + (y - x) * ratio) for x, y in zip(a, b))
