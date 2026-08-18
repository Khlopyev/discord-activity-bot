"""Тесты рендера карточки: проверяем, что PNG собирается во всех состояниях."""

from io import BytesIO

import pytest
from PIL import Image

from bot.card import CHART_DAYS, HEIGHT, WIDTH, CardData, render_card


def make_card(**overrides) -> CardData:
    defaults = dict(
        display_name="Тестовый Участник",
        guild_name="Крестоносцы",
        avatar_png=None,
        rank=2,
        rank_total=17,
        score=1234.0,
        voice_all=90_000,
        voice_week=12_000,
        voice_month=45_000,
        messages_all=980,
        messages_week=120,
        messages_month=400,
        stream_all=3600,
        favourite_channel="voice IV",
        series=[(str(i + 1), float(i * 40)) for i in range(CHART_DAYS)],
        footer="За всё время · сутки по Europe/Moscow",
    )
    defaults.update(overrides)
    return CardData(**defaults)


def render(card: CardData) -> Image.Image:
    png = render_card(card)
    image = Image.open(BytesIO(png))
    assert image.format == "PNG"
    return image


def test_card_has_expected_dimensions():
    assert render(make_card()).size == (WIDTH, HEIGHT)


@pytest.mark.parametrize("rank", [1, 2, 3, 4, None])
def test_every_rank_renders(rank):
    # Ранги 1-3 подсвечиваются своими цветами, остальные — акцентом по умолчанию.
    assert render(make_card(rank=rank, rank_total=17 if rank else None)).size == (WIDTH, HEIGHT)


def test_empty_statistics_render():
    card = make_card(
        rank=None,
        rank_total=None,
        score=0.0,
        voice_all=0, voice_week=0, voice_month=0,
        messages_all=0, messages_week=0, messages_month=0,
        stream_all=0,
        favourite_channel=None,
        series=[(str(i + 1), 0.0) for i in range(CHART_DAYS)],
    )
    assert render(card).size == (WIDTH, HEIGHT)


def test_long_name_does_not_break_render():
    assert render(make_card(display_name="О" * 200)).size == (WIDTH, HEIGHT)


def test_name_without_letters_falls_back_to_placeholder():
    # Ник из одних символов: заглушка аватара не должна падать на поиске буквы.
    assert render(make_card(display_name="???")).size == (WIDTH, HEIGHT)


def test_broken_avatar_falls_back_to_placeholder():
    assert render(make_card(avatar_png=b"not actually a png")).size == (WIDTH, HEIGHT)


def test_real_avatar_is_used():
    buffer = BytesIO()
    Image.new("RGB", (256, 256), (200, 40, 40)).save(buffer, format="PNG")
    assert render(make_card(avatar_png=buffer.getvalue())).size == (WIDTH, HEIGHT)


def test_empty_series_renders():
    assert render(make_card(series=[])).size == (WIDTH, HEIGHT)
