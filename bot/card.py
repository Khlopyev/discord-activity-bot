"""Рендер карточки профиля (раздел 3.5 брифа).

Чистый Pillow, без внешних зависимостей и headless-браузера. Рендер блокирующий,
поэтому вызывается через `asyncio.to_thread` — см. `StatsService.profile_card`.

Дизайн: тёмная карточка под интерфейс Discord, акцент — сине-фиолетовый градиент.
Ранги 1-3 подсвечиваются золотом/серебром/бронзой в кольце аватара и бейдже.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 940, 540
PAD = 36

# Вертикальная разметка: панели -> строка деталей -> график -> подпись.
PANELS_TOP = 196
PANELS_HEIGHT = 112
EXTRAS_TOP = 324
CHART_TOP = 382
CHART_BOTTOM = 470

BG_TOP = (27, 29, 35)
BG_BOTTOM = (18, 19, 23)
PANEL = (32, 34, 42)
PANEL_EDGE = (44, 47, 57)
TEXT = (242, 243, 245)
TEXT_MUTED = (150, 157, 168)
TEXT_DIM = (108, 114, 126)
ACCENT = (88, 101, 242)
ACCENT_SOFT = (139, 92, 246)
BAR_IDLE = (52, 56, 68)

RANK_COLOURS = {1: (240, 178, 50), 2: (188, 192, 200), 3: (205, 127, 50)}

CHART_DAYS = 14

FONT_CANDIDATES_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
FONT_CANDIDATES_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


@dataclass
class CardData:
    """Всё, что рисуется на карточке. Собирается в `StatsService`."""

    display_name: str
    guild_name: str
    avatar_png: bytes | None
    rank: int | None
    rank_total: int | None
    score: float
    voice_all: int
    voice_week: int
    voice_month: int
    messages_all: int
    messages_week: int
    messages_month: int
    stream_all: int
    favourite_channel: str | None
    # (подпись дня, значение) — ровно CHART_DAYS точек, слева направо
    series: list[tuple[str, float]]
    footer: str
    # (название игры, секунды) по данным Rich Presence; None — если трекинг игр
    # выключен или участник скрыл активность в настройках приватности.
    top_game: tuple[str, int] | None = None
    # Готовая подпись самого активного времени, например «вторник, 21:00–22:00».
    peak_slot: str | None = None


class _Fonts:
    """Ленивый резолвер шрифтов: системные пути или переопределение из окружения."""

    def __init__(self) -> None:
        self._regular = self._resolve("FONT_REGULAR_PATH", FONT_CANDIDATES_REGULAR)
        self._bold = self._resolve("FONT_BOLD_PATH", FONT_CANDIDATES_BOLD) or self._regular
        if self._regular is None:
            log.warning(
                "Не найден ни один TTF-шрифт — карточка будет отрисована встроенным "
                "растровым шрифтом и выглядеть плохо. Задайте FONT_REGULAR_PATH."
            )
        self._cache: dict[tuple[bool, int], ImageFont.ImageFont] = {}

    @staticmethod
    def _resolve(env_var: str, candidates: tuple[str, ...]) -> str | None:
        override = os.getenv(env_var)
        if override and Path(override).is_file():
            return override
        for path in candidates:
            if Path(path).is_file():
                return path
        return None

    def get(self, size: int, *, bold: bool = False) -> ImageFont.ImageFont:
        key = (bold, size)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        path = self._bold if bold else self._regular
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        self._cache[key] = font
        return font


fonts = _Fonts()


def render_card(data: CardData) -> bytes:
    """Отрисовать карточку и вернуть PNG. Блокирующая операция."""
    image = _background()
    draw = ImageDraw.Draw(image)

    accent = RANK_COLOURS.get(data.rank or 0, ACCENT)

    _draw_header(image, draw, data, accent)
    _draw_stat_panels(draw, data)
    _draw_extras(draw, data)
    _draw_chart(draw, data, accent)

    draw.text((PAD, HEIGHT - 28), data.footer, font=fonts.get(14), fill=TEXT_DIM)

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --- слои ---


def _background() -> Image.Image:
    base = Image.new("RGB", (WIDTH, HEIGHT), BG_BOTTOM)
    top = Image.new("RGB", (1, HEIGHT))
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        top.putpixel(
            (0, y),
            tuple(int(a + (b - a) * ratio) for a, b in zip(BG_TOP, BG_BOTTOM)),
        )
    base.paste(top.resize((WIDTH, HEIGHT)), (0, 0))

    # Мягкое свечение в углу — чтобы фон не выглядел плоской заливкой.
    glow = Image.new("RGB", (WIDTH, HEIGHT), BG_TOP)
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-180, -260, 520, 200), fill=ACCENT)
    glow_draw.ellipse((WIDTH - 260, HEIGHT - 200, WIDTH + 220, HEIGHT + 180), fill=ACCENT_SOFT)
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    return Image.blend(base, glow, 0.18)


def _draw_header(
    image: Image.Image, draw: ImageDraw.ImageDraw, data: CardData, accent: tuple[int, int, int]
) -> None:
    avatar_size = 132
    ring = 5
    box = (PAD, PAD, PAD + avatar_size, PAD + avatar_size)

    draw.ellipse(
        (box[0] - ring, box[1] - ring, box[2] + ring, box[3] + ring),
        fill=accent,
    )
    avatar = _circular_avatar(data.avatar_png, avatar_size, data.display_name)
    image.paste(avatar, (box[0], box[1]), avatar)

    text_x = box[2] + 32
    name = _fit_text(draw, data.display_name, fonts.get(38, bold=True), WIDTH - text_x - 190)
    draw.text((text_x, PAD + 22), name, font=fonts.get(38, bold=True), fill=TEXT)

    if data.rank is not None and data.rank_total:
        subtitle = f"Место {data.rank} из {data.rank_total} · {data.guild_name}"
    else:
        subtitle = data.guild_name
    subtitle = _fit_text(draw, subtitle, fonts.get(19), WIDTH - text_x - 190)
    draw.text((text_x, PAD + 76), subtitle, font=fonts.get(19), fill=TEXT_MUTED)

    _draw_rank_badge(draw, data, accent)


def _draw_rank_badge(
    draw: ImageDraw.ImageDraw, data: CardData, accent: tuple[int, int, int]
) -> None:
    label = f"#{data.rank}" if data.rank is not None else "—"
    font = fonts.get(44, bold=True)
    right = WIDTH - PAD
    box = (right - 150, PAD + 8, right, PAD + 100)

    draw.rounded_rectangle(box, radius=22, fill=PANEL, outline=accent, width=2)
    _centred_text(draw, label, font, box[0], box[2], box[1] + 16, accent)
    _centred_text(
        draw, "в рейтинге", fonts.get(13), box[0], box[2], box[1] + 66, TEXT_DIM
    )


def _draw_stat_panels(draw: ImageDraw.ImageDraw, data: CardData) -> None:
    from .timeutil import format_duration

    panels = (
        (
            "В ГОЛОСОВЫХ",
            format_duration(data.voice_all),
            f"неделя {format_duration(data.voice_week)}"
            f"  ·  месяц {format_duration(data.voice_month)}",
        ),
        (
            "СООБЩЕНИЙ",
            _number(data.messages_all),
            f"неделя {_number(data.messages_week)}"
            f"  ·  месяц {_number(data.messages_month)}",
        ),
        ("РЕЙТИНГ", f"{data.score:,.0f}".replace(",", " "), "очков активности"),
    )

    gap = 16
    width = (WIDTH - 2 * PAD - 2 * gap) // 3
    top = PANELS_TOP
    height = PANELS_HEIGHT

    for index, (label, value, hint) in enumerate(panels):
        left = PAD + index * (width + gap)
        draw.rounded_rectangle(
            (left, top, left + width, top + height),
            radius=16,
            fill=PANEL,
            outline=PANEL_EDGE,
            width=1,
        )
        draw.text((left + 20, top + 16), label, font=fonts.get(13, bold=True), fill=TEXT_DIM)
        value_font = fonts.get(31, bold=True)
        draw.text(
            (left + 20, top + 38),
            _fit_text(draw, value, value_font, width - 40),
            font=value_font,
            fill=TEXT,
        )
        hint_font = fonts.get(14)
        draw.text(
            (left + 20, top + 80),
            _fit_text(draw, hint, hint_font, width - 40),
            font=hint_font,
            fill=TEXT_MUTED,
        )


def _draw_extras(draw: ImageDraw.ImageDraw, data: CardData) -> None:
    from .timeutil import format_duration

    parts = []
    if data.top_game:
        name, seconds = data.top_game
        parts.append(f"Чаще всего играет: {name} ({format_duration(seconds)})")
    if data.stream_all:
        parts.append(f"Стримил: {format_duration(data.stream_all)}")
    if data.favourite_channel:
        parts.append(f"Любимый канал: {data.favourite_channel}")
    if data.peak_slot:
        parts.append(f"Пик: {data.peak_slot}")
    if not parts:
        return

    font = fonts.get(15)
    line = "   ·   ".join(parts)
    draw.text(
        (PAD, EXTRAS_TOP), _fit_text(draw, line, font, WIDTH - 2 * PAD), font=font, fill=TEXT_MUTED
    )


def _draw_chart(
    draw: ImageDraw.ImageDraw, data: CardData, accent: tuple[int, int, int]
) -> None:
    top, bottom = CHART_TOP, CHART_BOTTOM
    left, right = PAD, WIDTH - PAD

    draw.text(
        (left, top - 26),
        f"АКТИВНОСТЬ ЗА ПОСЛЕДНИЕ {len(data.series)} ДНЕЙ",
        font=fonts.get(13, bold=True),
        fill=TEXT_DIM,
    )
    draw.line((left, bottom, right, bottom), fill=PANEL_EDGE, width=1)

    if not data.series:
        return

    peak = max(value for _label, value in data.series)
    slot = (right - left) / len(data.series)
    bar_width = max(6, int(slot * 0.58))
    height = bottom - top

    for index, (label, value) in enumerate(data.series):
        centre = left + slot * (index + 0.5)
        x0 = int(centre - bar_width / 2)
        x1 = x0 + bar_width
        is_today = index == len(data.series) - 1

        if peak > 0 and value > 0:
            bar_height = max(4, int(height * (value / peak)))
            colour = accent if is_today else _mix(accent, PANEL, 0.35)
            draw.rounded_rectangle(
                (x0, bottom - bar_height, x1, bottom), radius=min(6, bar_width // 2), fill=colour
            )
        else:
            # Пустой день — короткий приглушённый штрих, иначе провалы читаются как обрыв.
            draw.rounded_rectangle((x0, bottom - 3, x1, bottom), radius=1, fill=BAR_IDLE)

        _centred_text(
            draw,
            label,
            fonts.get(12),
            x0 - 6,
            x1 + 6,
            bottom + 8,
            TEXT if is_today else TEXT_DIM,
        )

    if peak > 0:
        caption = f"пик {peak:,.0f}".replace(",", " ")
        font = fonts.get(12)
        draw.text(
            (right - _text_width(draw, caption, font), top - 25),
            caption,
            font=font,
            fill=TEXT_DIM,
        )


# --- утилиты ---


def _circular_avatar(png: bytes | None, size: int, display_name: str) -> Image.Image:
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4, size * 4), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)

    if png:
        try:
            source = Image.open(BytesIO(png)).convert("RGB").resize((size, size), Image.LANCZOS)
        except Exception:
            log.warning("Не удалось прочитать аватар, рисую заглушку", exc_info=True)
            source = _avatar_placeholder(size, display_name)
    else:
        source = _avatar_placeholder(size, display_name)

    avatar = Image.new("RGBA", (size, size))
    avatar.paste(source, (0, 0))
    avatar.putalpha(mask)
    return avatar


def _avatar_placeholder(size: int, display_name: str) -> Image.Image:
    image = Image.new("RGB", (size, size), PANEL)
    draw = ImageDraw.Draw(image)
    # Ники часто начинаются с тегов клана вроде «[KRST] …» — берём первую букву.
    letter = next((c for c in display_name if c.isalnum()), "?").upper()
    font = fonts.get(int(size * 0.5), bold=True)
    _centred_text(draw, letter, font, 0, size, int(size * 0.22), TEXT_MUTED)
    return image


def _centred_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    left: float,
    right: float,
    top: float,
    fill: tuple[int, int, int],
) -> None:
    width = _text_width(draw, text, font)
    draw.text((left + (right - left - width) / 2, top), text, font=font, fill=fill)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> float:
    return draw.textlength(text, font=font)


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: float
) -> str:
    """Обрезать строку многоточием, чтобы длинный ник не уехал за край карточки."""
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and _text_width(draw, trimmed + ellipsis, font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ellipsis


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _mix(
    a: tuple[int, int, int], b: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    return tuple(int(x + (y - x) * ratio) for x, y in zip(a, b))
