"""Тесты рендера карточки: проверяем, что PNG собирается во всех состояниях."""

import pathlib
from io import BytesIO

import pytest
from PIL import Image

from bot.card import (
    CHART_DAYS,
    FONT_CANDIDATES_REGULAR,
    HEIGHT,
    WIDTH,
    CardData,
    _Fonts,
    render_card,
)


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


# --- переопределение шрифта из окружения ---


def fake_font(tmp_path, name="fake.ttf"):
    """Файл, который проходит проверку is_file(); грузить его мы не будем."""
    path = tmp_path / name
    path.write_bytes(b"not a real font")
    return str(path)


def real_system_font(tmp_path, name="override.ttf") -> str:
    """Копия настоящего системного шрифта — её можно реально загрузить."""
    source = next(
        (path for path in FONT_CANDIDATES_REGULAR if pathlib.Path(path).is_file()), None
    )
    if source is None:
        pytest.skip("на машине нет ни одного TTF — переопределять нечем")
    copy = tmp_path / name
    copy.write_bytes(pathlib.Path(source).read_bytes())
    return str(copy)


def test_font_override_from_env_is_actually_used(tmp_path, monkeypatch):
    """Порядок как в бою: экземпляр создаётся на импорте, .env читается позже.

    Резолв делался в __init__, то есть до load_dotenv() внутри
    Config.from_env(), и FONT_REGULAR_PATH из .env молча не работал. Проверяем
    через публичный get(), а не через внутренности: так тест падает по сути —
    взят не тот шрифт, — а не на отсутствующем методе.
    """
    monkeypatch.delenv("FONT_REGULAR_PATH", raising=False)
    fonts = _Fonts()                                   # <- импорт модуля

    override = real_system_font(tmp_path)
    monkeypatch.setenv("FONT_REGULAR_PATH", override)  # <- load_dotenv()

    fonts.get(14)
    assert fonts._regular == override


def test_bold_override_is_independent(tmp_path, monkeypatch):
    regular = fake_font(tmp_path, "regular.ttf")
    bold = fake_font(tmp_path, "bold.ttf")
    monkeypatch.setenv("FONT_REGULAR_PATH", regular)
    monkeypatch.setenv("FONT_BOLD_PATH", bold)

    fonts = _Fonts()
    fonts._ensure_resolved()
    assert (fonts._regular, fonts._bold) == (regular, bold)


def test_bold_falls_back_to_regular_when_nothing_bold_exists(tmp_path, monkeypatch):
    """Если жирного нет нигде, берётся обычное начертание, а не None.

    Системные кандидаты вычищаются: на машине с установленным Segoe UI Semibold
    до этой ветки иначе не добраться.
    """
    monkeypatch.setattr("bot.card.FONT_CANDIDATES_BOLD", ())
    regular = fake_font(tmp_path, "regular.ttf")
    monkeypatch.setenv("FONT_REGULAR_PATH", regular)
    monkeypatch.delenv("FONT_BOLD_PATH", raising=False)

    fonts = _Fonts()
    fonts._ensure_resolved()
    assert fonts._bold == regular


def test_nonexistent_override_falls_back_to_system(tmp_path, monkeypatch):
    """Опечатка в пути не должна ронять рендер — только терять переопределение."""
    monkeypatch.setenv("FONT_REGULAR_PATH", str(tmp_path / "нет-такого.ttf"))
    fonts = _Fonts()
    fonts._ensure_resolved()
    assert fonts._regular != str(tmp_path / "нет-такого.ttf")


def test_resolution_happens_once(tmp_path, monkeypatch):
    first = fake_font(tmp_path, "first.ttf")
    monkeypatch.setenv("FONT_REGULAR_PATH", first)
    fonts = _Fonts()
    fonts._ensure_resolved()

    monkeypatch.setenv("FONT_REGULAR_PATH", fake_font(tmp_path, "second.ttf"))
    fonts._ensure_resolved()
    assert fonts._regular == first, "повторный резолв на горячем пути не нужен"
