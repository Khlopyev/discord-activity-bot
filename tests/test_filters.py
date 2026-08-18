"""Тесты правил исключения каналов и участников."""

from dataclasses import dataclass, field

import pytest

from bot.filters import is_tracked_channel, is_tracked_user

GUILD = 1


@dataclass
class FakeChannel:
    id: int
    guild: "FakeGuild | None" = None
    parent_id: int | None = None


@dataclass
class FakeGuild:
    id: int = GUILD
    afk_channel: FakeChannel | None = None


@dataclass
class FakeUser:
    id: int
    bot: bool = False


@dataclass
class FakeConfig:
    excluded_channel_ids: frozenset = field(default_factory=frozenset)
    exclude_afk_channel: bool = True


class FakeSettings:
    def __init__(self, excluded=(), optouts=()) -> None:
        self._excluded = frozenset(excluded)
        self._optouts = set(optouts)

    def excluded_channels(self, guild_id: int) -> frozenset:
        return self._excluded

    def is_opted_out(self, guild_id: int, user_id: int) -> bool:
        return (guild_id, user_id) in self._optouts


def channel(channel_id: int, **kwargs) -> FakeChannel:
    guild = kwargs.pop("guild", FakeGuild())
    return FakeChannel(id=channel_id, guild=guild, **kwargs)


def test_ordinary_channel_is_tracked():
    assert is_tracked_channel(channel(100), FakeConfig()) is True


def test_none_channel_is_not_tracked():
    assert is_tracked_channel(None, FakeConfig()) is False


def test_channel_excluded_by_environment():
    config = FakeConfig(excluded_channel_ids=frozenset({100}))
    assert is_tracked_channel(channel(100), config) is False
    assert is_tracked_channel(channel(200), config) is True


def test_channel_excluded_by_guild_settings():
    settings = FakeSettings(excluded=[100])
    assert is_tracked_channel(channel(100), FakeConfig(), settings) is False
    assert is_tracked_channel(channel(200), FakeConfig(), settings) is True


def test_thread_inherits_parent_exclusion():
    settings = FakeSettings(excluded=[100])
    thread = channel(999, parent_id=100)
    assert is_tracked_channel(thread, FakeConfig(), settings) is False


def test_afk_channel_excluded_by_default():
    afk = FakeChannel(id=300)
    guild = FakeGuild(afk_channel=afk)
    assert is_tracked_channel(channel(300, guild=guild), FakeConfig()) is False
    assert is_tracked_channel(channel(400, guild=guild), FakeConfig()) is True


def test_afk_exclusion_can_be_disabled():
    guild = FakeGuild(afk_channel=FakeChannel(id=300))
    config = FakeConfig(exclude_afk_channel=False)
    assert is_tracked_channel(channel(300, guild=guild), config) is True


def test_env_and_guild_exclusions_combine():
    config = FakeConfig(excluded_channel_ids=frozenset({100}))
    settings = FakeSettings(excluded=[200])
    assert is_tracked_channel(channel(100), config, settings) is False
    assert is_tracked_channel(channel(200), config, settings) is False
    assert is_tracked_channel(channel(300), config, settings) is True


def test_bots_are_never_tracked():
    assert is_tracked_user(GUILD, FakeUser(id=5, bot=True)) is False


def test_opted_out_user_is_not_tracked():
    settings = FakeSettings(optouts=[(GUILD, 5)])
    assert is_tracked_user(GUILD, FakeUser(id=5), settings) is False
    assert is_tracked_user(GUILD, FakeUser(id=6), settings) is True


def test_optout_does_not_leak_between_guilds():
    settings = FakeSettings(optouts=[(GUILD, 5)])
    assert is_tracked_user(999, FakeUser(id=5), settings) is True


def test_user_tracked_without_settings_store():
    assert is_tracked_user(GUILD, FakeUser(id=5)) is True
