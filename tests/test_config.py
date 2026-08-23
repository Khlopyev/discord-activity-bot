"""Разбор переменных окружения (bot/config.py).

Значения весов попадают в формулу комбинированного счёта, по которой
сортируется лидерборд, поэтому мусор в них должен падать на старте, а не
искажать выдачу молча.
"""

from __future__ import annotations

import pytest

from bot.config import _bool, _float, _id_set, _int, _activity_types


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("W", "N", "FLAG", "IDS", "TYPES"):
        monkeypatch.delenv(name, raising=False)


def test_missing_and_blank_fall_back_to_default(monkeypatch):
    assert _float("W", 1.5) == 1.5
    monkeypatch.setenv("W", "   ")
    assert _float("W", 1.5) == 1.5


def test_value_is_parsed_and_trimmed(monkeypatch):
    monkeypatch.setenv("W", " 2.5 ")
    assert _float("W", 1.0) == 2.5


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_non_finite_weights_are_rejected(monkeypatch, raw):
    """float() принимает эти строки молча, а лидерборд после них не сортируется."""
    monkeypatch.setenv("W", raw)
    with pytest.raises(RuntimeError, match="конечным числом"):
        _float("W", 1.0)


def test_garbage_is_rejected(monkeypatch):
    monkeypatch.setenv("W", "полтора")
    with pytest.raises(RuntimeError, match="числом"):
        _float("W", 1.0)


def test_minimum_is_enforced(monkeypatch):
    monkeypatch.setenv("W", "-1")
    with pytest.raises(RuntimeError, match=">= 0"):
        _float("W", 1.0, minimum=0.0)


def test_zero_weight_is_allowed(monkeypatch):
    """Ноль — законный способ убрать одну из составляющих из формулы."""
    monkeypatch.setenv("W", "0")
    assert _float("W", 1.0, minimum=0.0) == 0.0


def test_int_bounds(monkeypatch):
    monkeypatch.setenv("N", "3")
    with pytest.raises(RuntimeError, match=">= 5"):
        _int("N", 60, minimum=5)
    monkeypatch.setenv("N", "5")
    assert _int("N", 60, minimum=5) == 5


def test_bool_accepts_common_spellings(monkeypatch):
    for raw in ("1", "true", "TRUE", "yes", "y", "on"):
        monkeypatch.setenv("FLAG", raw)
        assert _bool("FLAG", False) is True
    for raw in ("0", "false", "no", "что угодно ещё"):
        monkeypatch.setenv("FLAG", raw)
        assert _bool("FLAG", True) is False


def test_id_set_splits_on_commas_and_semicolons(monkeypatch):
    monkeypatch.setenv("IDS", "1, 2;3 ,")
    assert _id_set("IDS") == frozenset({1, 2, 3})


def test_id_set_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("IDS", "1,abc")
    with pytest.raises(RuntimeError, match="списком ID"):
        _id_set("IDS")


def test_activity_types_defaults_to_playing():
    assert _activity_types("TYPES") == frozenset({"playing"})


def test_activity_types_rejects_unknown(monkeypatch):
    monkeypatch.setenv("TYPES", "playing,sleeping")
    with pytest.raises(RuntimeError, match="sleeping"):
        _activity_types("TYPES")
