"""Тесты настроек гильдии, отказа от трекинга и удаления данных."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.config import Config
from bot.db import Database
from bot.filters import is_tracked_channel
from bot.main import ActivityBot
from bot.settings import EXCLUDED_CHANNELS, SUMMARY_CHANNEL, SettingsStore

UTC = ZoneInfo("UTC")
GUILD, OTHER_GUILD = 1, 2
ALICE, BOB = 10, 20


def dt(day, hour=0):
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(str(tmp_path / "settings.db"), UTC)
    await db.connect()
    await db.upsert_guild(GUILD, "test")
    await db.upsert_guild(OTHER_GUILD, "other")
    settings = SettingsStore(db)
    await settings.load()
    yield settings
    await db.close()


@pytest.mark.asyncio
async def test_settings_round_trip(store):
    await store.update(GUILD, {SUMMARY_CHANNEL: 555})
    assert (await store.get(GUILD))[SUMMARY_CHANNEL] == 555

    # Патч сливается с существующими настройками, а не затирает их.
    await store.update(GUILD, {"other_key": "value"})
    settings = await store.get(GUILD)
    assert settings[SUMMARY_CHANNEL] == 555
    assert settings["other_key"] == "value"


@pytest.mark.asyncio
async def test_settings_survive_reload(store):
    await store.update(GUILD, {SUMMARY_CHANNEL: 777})
    store._guilds.clear()
    assert (await store.get(GUILD))[SUMMARY_CHANNEL] == 777


@pytest.mark.asyncio
async def test_channel_exclusions(store):
    assert store.excluded_channels(GUILD) == frozenset()

    assert await store.exclude_channel(GUILD, 100) is True
    assert await store.exclude_channel(GUILD, 100) is False  # повторно — не дублируем
    assert await store.exclude_channel(GUILD, 200) is True
    assert store.excluded_channels(GUILD) == frozenset({100, 200})

    assert await store.include_channel(GUILD, 100) is True
    assert await store.include_channel(GUILD, 100) is False
    assert store.excluded_channels(GUILD) == frozenset({200})

    # Исключения одного сервера не протекают на другой.
    assert store.excluded_channels(OTHER_GUILD) == frozenset()


@pytest.mark.asyncio
async def test_exclusions_persisted_as_list(store):
    await store.exclude_channel(GUILD, 100)
    raw = await store.db.get_guild_settings(GUILD)
    assert raw[EXCLUDED_CHANNELS] == [100]


@pytest.mark.asyncio
async def test_optout_is_per_guild(store):
    await store.set_optout(GUILD, ALICE, True)
    assert store.is_opted_out(GUILD, ALICE) is True
    assert store.is_opted_out(OTHER_GUILD, ALICE) is False
    assert store.is_opted_out(GUILD, BOB) is False

    await store.set_optout(GUILD, ALICE, False)
    assert store.is_opted_out(GUILD, ALICE) is False


@pytest.mark.asyncio
async def test_optout_survives_reload(store):
    await store.set_optout(GUILD, ALICE, True)
    await store.load()
    assert store.is_opted_out(GUILD, ALICE) is True


@pytest.mark.asyncio
async def test_purge_user_removes_only_that_user(store):
    db = store.db
    await db.upsert_users([(ALICE, "alice", False), (BOB, "bob", False)])
    for user in (ALICE, BOB):
        session = await db.open_voice_session(
            guild_id=GUILD, user_id=user, channel_id=5, joined_at=dt(17, 10), is_stream=False
        )
        await db.close_session(session, dt(17, 11), min_session_seconds=10)
        await db.add_message_counts([(GUILD, user, 5, "2026-08-17", 3, 30)])

    removed = await db.purge_user(GUILD, ALICE)
    assert removed > 0

    weights = {"voice_weight": 1.0, "message_weight": 1.0}
    assert await db.user_totals(GUILD, ALICE, **weights) is None
    assert (await db.user_totals(GUILD, BOB, **weights)).voice_seconds == 3600


@pytest.mark.asyncio
async def test_purge_guild_keeps_other_guild(store):
    db = store.db
    await db.upsert_users([(ALICE, "alice", False)])
    for guild in (GUILD, OTHER_GUILD):
        session = await db.open_voice_session(
            guild_id=guild, user_id=ALICE, channel_id=5, joined_at=dt(17, 10), is_stream=False
        )
        await db.close_session(session, dt(17, 11), min_session_seconds=10)

    await db.purge_guild(GUILD)

    weights = {"voice_weight": 1.0, "message_weight": 1.0}
    assert await db.user_totals(GUILD, ALICE, **weights) is None
    assert (await db.user_totals(OTHER_GUILD, ALICE, **weights)).voice_seconds == 3600


@pytest.mark.asyncio
async def test_purge_guild_keeps_settings_and_optouts(store):
    await store.exclude_channel(GUILD, 100)
    await store.set_optout(GUILD, ALICE, True)

    await store.db.purge_guild(GUILD)

    store._guilds.clear()
    await store.load()
    assert store.excluded_channels(GUILD) == frozenset()  # кэш ещё не прогрет
    assert (await store.get(GUILD))[EXCLUDED_CHANNELS] == [100]
    assert store.is_opted_out(GUILD, ALICE) is True


@pytest.mark.asyncio
async def test_export_rows_scoped_to_guild(store):
    db = store.db
    await db.add_message_counts(
        [
            (GUILD, ALICE, 5, "2026-08-17", 3, 30),
            (OTHER_GUILD, ALICE, 5, "2026-08-17", 9, 90),
        ]
    )
    headers, rows = await db.export_rows(GUILD, "message_events_daily")
    assert "message_count" in headers
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_export_rejects_unknown_table(store):
    with pytest.raises(ValueError, match="не разрешена"):
        await store.db.export_rows(GUILD, "users; DROP TABLE users")


# --- прогрев кэша для сервера, на который бота пригласили заново ---


def make_config(database_path: str, **overrides) -> Config:
    from dataclasses import fields

    values = dict(
        token="x", command_prefix="!", database_path=database_path, timezone=UTC,
        timezone_name="UTC", excluded_channel_ids=frozenset(), exclude_afk_channel=True,
        min_session_seconds=10, voice_flush_interval=60, message_flush_interval=30,
        leaderboard_cache_ttl=60, track_char_count=True, enable_slash_commands=False,
        enable_presence_tracking=False, tracked_activity_types=frozenset({"playing"}),
        presence_flush_interval=60, combined_voice_weight=1.0,
        combined_message_weight=2.0, raw_retention_days=90,
    )
    assert set(values) == {f.name for f in fields(Config)}
    values.update(overrides)
    return Config(**values)


class RejoinedGuild:
    """Сервер, на котором бот уже был: исключения лежат в базе."""

    def __init__(self) -> None:
        self.id = GUILD
        self.name = "test"
        self.afk_channel = None


class ExcludedChannel:
    def __init__(self, guild) -> None:
        self.id = 100
        self.guild = guild


@pytest.mark.asyncio
async def test_rejoining_a_guild_restores_its_exclusions(tmp_path):
    """Приглашение бота заново не должно возвращать в трекинг исключённый канал.

    Настройки прогреваются на on_ready по списку уже известных серверов.
    Сервер, появившийся позже, в этот список не попадал: в базе исключения
    лежали, а excluded_channels() отдавал пустоту — и канал считался снова.
    """
    path = str(tmp_path / "rejoin.db")

    # Первый заход бота: админ исключает канал.
    db = Database(path, UTC)
    await db.connect()
    await db.upsert_guild(GUILD, "test")
    settings = SettingsStore(db)
    await settings.load()
    await settings.prime([GUILD])
    await settings.exclude_channel(GUILD, 100)
    await db.close()

    # Бота удалили и пригласили заново: на on_ready сервера ещё нет.
    bot = ActivityBot(make_config(path))
    await bot.db.connect()
    await bot.settings.load()
    await bot.settings.prime([])

    guild = RejoinedGuild()
    channel = ExcludedChannel(guild)
    await bot.on_guild_join(guild)

    try:
        assert set(bot.settings.excluded_channels(GUILD)) == {100}
        assert is_tracked_channel(channel, bot.config, bot.settings) is False
    finally:
        await bot.db.close()
