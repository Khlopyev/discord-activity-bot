"""Кэш лидербордов (bot/stats.py) и его сброс после удаления данных.

Кэш жил только на TTL и не сбрасывался ничем. После `/optout` человек получал
ответ «вся накопленная статистика удалена» и продолжал висеть в лидерборде до
конца LEADERBOARD_CACHE_TTL — то есть ровно там, откуда просил себя убрать.
"""

from __future__ import annotations

import time
from dataclasses import fields
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.config import Config
from bot.cogs.admin import Admin
from bot.db import Database
from bot.stats import StatsService

UTC = ZoneInfo("UTC")
GUILD, OTHER_GUILD = 1, 2
ALICE = 10


def dt(day, hour=0):
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


def make_config(**overrides) -> Config:
    values = dict(
        token="x", command_prefix="!", database_path=":memory:", timezone=UTC,
        timezone_name="UTC", excluded_channel_ids=frozenset(), exclude_afk_channel=True,
        min_session_seconds=10, voice_flush_interval=60, message_flush_interval=30,
        leaderboard_cache_ttl=60, track_char_count=True, enable_slash_commands=True,
        enable_presence_tracking=False, tracked_activity_types=frozenset({"playing"}),
        presence_flush_interval=60, combined_voice_weight=1.0,
        combined_message_weight=2.0, raw_retention_days=90,
    )
    # Ловим появление нового поля в Config: молча собранный конфиг с дефолтом
    # из теста хуже, чем падение здесь.
    assert set(values) == {f.name for f in fields(Config)}
    values.update(overrides)
    return Config(**values)


class FakeGuild:
    """Минимум, который трогает leaderboard_embed."""

    def __init__(self, guild_id: int = GUILD) -> None:
        self.id = guild_id
        self.name = "test"

    def get_member(self, user_id):
        return None


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "stats.db"), UTC)
    await database.connect()
    await database.upsert_guild(GUILD, "test")
    await database.upsert_users([(ALICE, "alice", False)])
    yield database
    await database.close()


async def give_alice_an_hour(db):
    session = await db.open_voice_session(
        guild_id=GUILD, user_id=ALICE, channel_id=100, joined_at=dt(18, 12), is_stream=False
    )
    await db.close_session(session, dt(18, 13), min_session_seconds=10)


@pytest.mark.asyncio
async def test_leaderboard_is_cached(db):
    """Кэш должен работать — иначе следующий тест ничего не доказывает."""
    stats = StatsService(db, make_config())
    await give_alice_an_hour(db)
    await stats.leaderboard_embed(FakeGuild(), "combined", "all", 10)
    assert stats._cache, "лидерборд не попал в кэш"


@pytest.mark.asyncio
async def test_leaderboard_is_fresh_after_purge(db):
    stats = StatsService(db, make_config())
    await give_alice_an_hour(db)

    before = await stats.leaderboard_embed(FakeGuild(), "combined", "all", 10)
    assert "alice" in before.description

    await db.purge_user(GUILD, ALICE)
    stats.invalidate(GUILD)

    after = await stats.leaderboard_embed(FakeGuild(), "combined", "all", 10)
    assert "alice" not in after.description


@pytest.mark.asyncio
async def test_invalidate_spares_other_guilds(db):
    """Сброс по одной гильдии не должен обнулять кэш соседних."""
    stats = StatsService(db, make_config())
    stats._cache = {
        ("top", GUILD, "combined", "all", 10): (time.monotonic() + 60, "свои"),
        ("top", OTHER_GUILD, "combined", "all", 10): (time.monotonic() + 60, "чужие"),
    }
    stats.invalidate(GUILD)
    assert list(stats._cache) == [("top", OTHER_GUILD, "combined", "all", 10)]


@pytest.mark.asyncio
async def test_invalidate_evicts_expired_entries(db):
    """Протухшие записи раньше оставались в словаре навсегда."""
    stats = StatsService(db, make_config())
    stats._cache = {
        ("top", OTHER_GUILD, "combined", "all", 10): (time.monotonic() - 1, "протухло"),
    }
    stats.invalidate(GUILD)
    assert stats._cache == {}


# --- проводка до самой команды ---


class FakeResponse:
    async def defer(self, **kwargs):
        pass


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, guild, user):
        self.guild = guild
        self.user = user
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "alice"


class FakeBot:
    def __init__(self, db, settings, stats):
        self.db = db
        self.settings = settings
        self.stats = stats

    def get_cog(self, name):
        return None


@pytest.mark.asyncio
async def test_optout_clears_the_leaderboard_cache(db):
    """Главное: приватность не должна зависеть от того, когда протухнет TTL."""
    from bot.settings import SettingsStore

    settings = SettingsStore(db)
    await settings.load()
    stats = StatsService(db, make_config())
    await give_alice_an_hour(db)

    guild = FakeGuild()
    before = await stats.leaderboard_embed(guild, "combined", "all", 10)
    assert "alice" in before.description

    cog = Admin(FakeBot(db, settings, stats))
    await Admin.optout.callback(cog, FakeInteraction(guild, FakeUser(ALICE)))

    after = await stats.leaderboard_embed(guild, "combined", "all", 10)
    assert "alice" not in after.description


# --- длина названий игр в эмбедах ---

# Документированные лимиты Discord.
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_TOTAL_LIMIT = 6000


def test_game_label_collapses_and_shortens():
    from bot.stats import MAX_GAME_NAME, game_label

    assert game_label("Dota 2") == "Dota 2"
    assert game_label("  Dota\n2  ") == "Dota 2"
    long_name = "И" * 300
    assert len(game_label(long_name)) == MAX_GAME_NAME
    assert game_label(long_name).endswith("…")


async def add_games(db, count: int, name_factory) -> None:
    await db.upsert_users([(200 + i, f"player{i}", False) for i in range(count)])
    for i in range(count):
        session = await db.open_presence_session(
            guild_id=GUILD, user_id=200 + i, activity_type="playing",
            activity_name=name_factory(i), started_at=dt(18, 10),
        )
        await db.close_presence_session(session, dt(18, 12), min_session_seconds=10)


def embed_size(embed) -> int:
    total = len(embed.title or "") + len(embed.description or "")
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    return total


@pytest.mark.asyncio
async def test_games_embed_stays_within_discord_limits(db):
    """Название игры задаёт стороннее приложение, и длина его ничем не ограничена.

    Discord отклоняет эмбед целиком, если описание длиннее 4096 символов, так
    что одна игра с длинным названием ломала всю команду. Худший случай —
    спецсимволы Markdown: escape_markdown их удваивает.
    """
    stats = StatsService(db, make_config(enable_presence_tracking=True))
    await add_games(db, 25, lambda i: "*_~`|" * 80 + f" #{i}")

    embed = await stats.games_embed(FakeGuild(), "all", 25)

    assert len(embed.description) <= EMBED_DESCRIPTION_LIMIT
    assert embed_size(embed) <= EMBED_TOTAL_LIMIT
    assert len(embed.description.splitlines()) == 25, "строки не должны потеряться"


@pytest.mark.asyncio
async def test_games_embed_keeps_ordinary_names_intact(db):
    """Опора: обрезка не должна трогать нормальные названия."""
    stats = StatsService(db, make_config(enable_presence_tracking=True))
    await add_games(db, 1, lambda i: "Dota 2")

    embed = await stats.games_embed(FakeGuild(), "all", 10)
    assert "Dota 2" in embed.description
