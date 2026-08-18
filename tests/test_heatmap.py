"""Тесты рендера тепловой карты."""

from io import BytesIO

import pytest
from PIL import Image

from bot.heatmap import (
    COLS,
    DISPLAY_TO_SQLITE,
    HEIGHT,
    ROWS,
    WEEKDAY_LABELS,
    WIDTH,
    HeatmapData,
    render_heatmap,
)


def make(slots) -> HeatmapData:
    return HeatmapData(
        title="Тепловая карта — Тест",
        subtitle="строки — дни недели, столбцы — часы",
        slots=slots,
        footer="За всё время · часы по Europe/Moscow",
    )


def render(data: HeatmapData) -> Image.Image:
    image = Image.open(BytesIO(render_heatmap(data)))
    assert image.format == "PNG"
    return image


def test_grid_covers_week_and_day():
    assert ROWS == 7 and COLS == 24


def test_display_order_starts_on_monday():
    # В БД неделя начинается с воскресенья (strftime('%w')), в выводе — с понедельника.
    assert WEEKDAY_LABELS[0] == "Пн" and DISPLAY_TO_SQLITE[0] == 1
    assert WEEKDAY_LABELS[-1] == "Вс" and DISPLAY_TO_SQLITE[-1] == 0
    assert sorted(DISPLAY_TO_SQLITE) == list(range(7))


def test_full_grid_renders():
    slots = {(w, h): float(w * 24 + h + 1) for w in range(7) for h in range(24)}
    assert render(make(slots)).size == (WIDTH, HEIGHT)


def test_sparse_grid_renders():
    assert render(make({(1, 21): 40.0, (2, 20): 5.0})).size == (WIDTH, HEIGHT)


def test_empty_grid_renders():
    # Пустая карта не должна падать на делении на пиковое значение.
    assert render(make({})).size == (WIDTH, HEIGHT)


def test_single_slot_renders():
    assert render(make({(3, 12): 1.0})).size == (WIDTH, HEIGHT)


@pytest.mark.parametrize("value", [0.0, 0.001, 1e9])
def test_extreme_values_render(value):
    assert render(make({(0, 0): value})).size == (WIDTH, HEIGHT)


def test_long_title_does_not_break_render():
    data = make({(1, 10): 5.0})
    data.title = "Тепловая карта — " + "О" * 120
    assert render(data).size == (WIDTH, HEIGHT)
