"""Тесты выбора релевантной активности из Rich Presence."""

from dataclasses import dataclass

import pytest

from bot.config import KNOWN_ACTIVITY_TYPES, _activity_types
from bot.cogs.presence import tracked_activity


@dataclass
class FakeType:
    name: str


@dataclass
class FakeActivity:
    type: FakeType
    name: str | None


@dataclass
class FakeMember:
    activities: tuple


def member(*activities: tuple[str, str | None]) -> FakeMember:
    return FakeMember(tuple(FakeActivity(FakeType(t), n) for t, n in activities))


PLAYING = frozenset({"playing"})


def test_playing_activity_is_picked():
    assert tracked_activity(member(("playing", "Dota 2")), PLAYING) == ("playing", "Dota 2")


def test_spotify_ignored_when_only_playing_tracked():
    assert tracked_activity(member(("listening", "Spotify")), PLAYING) is None


def test_first_relevant_activity_wins():
    # Слушает музыку и одновременно играет — считаем игру.
    who = member(("listening", "Spotify"), ("playing", "Valorant"))
    assert tracked_activity(who, PLAYING) == ("playing", "Valorant")


def test_no_activities_gives_none():
    assert tracked_activity(member(), PLAYING) is None


def test_activity_without_name_ignored():
    assert tracked_activity(member(("playing", None)), PLAYING) is None


def test_custom_status_type_is_not_tracked():
    # У кастомного статуса тип custom — в список отслеживаемых он не входит.
    assert tracked_activity(member(("custom", "занят")), PLAYING) is None


def test_extra_types_can_be_enabled():
    types = frozenset({"playing", "listening"})
    assert tracked_activity(member(("listening", "Spotify")), types) == ("listening", "Spotify")


def test_activity_types_config_defaults_to_playing(monkeypatch):
    monkeypatch.delenv("TRACKED_ACTIVITY_TYPES", raising=False)
    assert _activity_types("TRACKED_ACTIVITY_TYPES") == frozenset({"playing"})


def test_activity_types_config_parses_list(monkeypatch):
    monkeypatch.setenv("TRACKED_ACTIVITY_TYPES", "playing, watching")
    assert _activity_types("TRACKED_ACTIVITY_TYPES") == frozenset({"playing", "watching"})


def test_activity_types_config_rejects_unknown(monkeypatch):
    monkeypatch.setenv("TRACKED_ACTIVITY_TYPES", "playing,tetris")
    with pytest.raises(RuntimeError, match="tetris"):
        _activity_types("TRACKED_ACTIVITY_TYPES")


def test_known_types_match_discord_naming():
    assert KNOWN_ACTIVITY_TYPES == {"playing", "streaming", "listening", "watching"}
