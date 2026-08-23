"""Трекинг сообщений: исключения не должны зависеть от прогрева кэша.

Голосовой трекер игнорирует события до синхронизации после старта, а
текстовый — нет, и правильно: пропущенное сообщение уже не вернуть. Но
исключения каналов читаются синхронно и до первой загрузки настроек выглядят
пустыми, так что в первые мгновения работы бота исключённый канал успевал
посчитаться.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
import pytest
import pytest_asyncio

from bot.config import Config
from bot.cogs.text import TextTracker
from bot.db import Database
from bot.settings import SettingsStore

UTC = ZoneInfo("UTC")
GUILD, CHANNEL, ALICE = 1, 100, 10


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
    assert set(values) == {f.name for f in fields(Config)}
    values.update(overrides)
    return Config(**values)


class FakeGuild:
    def __init__(self) -> None:
        self.id = GUILD
        self.name = "test"
        self.afk_channel = None


class FakeChannel:
    def __init__(self, guild, channel_id: int = CHANNEL) -> None:
        self.id = channel_id
        self.guild = guild


class FakeAuthor:
    def __init__(self) -> None:
        self.id = ALICE
        self.bot = False

    def __str__(self) -> str:
        return "alice"


class FakeMessage:
    def __init__(self, guild, channel, content: str = "привет") -> None:
        self.guild = guild
        self.channel = channel
        self.author = FakeAuthor()
        self.content = content
        self.type = discord.MessageType.default
        self.created_at = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


class FakeBot:
    def __init__(self, config, db, settings) -> None:
        self.config = config
        self.db = db
        self.settings = settings

    async def wait_until_ready(self) -> None:
        await asyncio.Event().wait()


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "text.db"), UTC)
    await database.connect()
    await database.upsert_guild(GUILD, "test")
    yield database
    await database.close()


@pytest_asyncio.fixture
async def cold_tracker(db):
    """Трекер с исключённым каналом и **непрогретым** кэшем настроек.

    Ровно то, что бывает сразу после старта: settings.load() кэш очистил,
    а prime() ещё не отработал.
    """
    warm = SettingsStore(db)
    await warm.load()
    await warm.prime([GUILD])
    await warm.exclude_channel(GUILD, CHANNEL)

    settings = SettingsStore(db)
    await settings.load()
    assert settings.excluded_channels(GUILD) == frozenset(), "кэш должен быть холодным"

    tracker = TextTracker(FakeBot(make_config(), db, settings))
    yield tracker
    tracker.cog_unload()


@pytest.mark.asyncio
async def test_excluded_channel_is_skipped_before_settings_are_primed(cold_tracker):
    guild = FakeGuild()
    await cold_tracker.on_message(FakeMessage(guild, FakeChannel(guild)))
    assert cold_tracker._buffer == {}, "сообщение из исключённого канала попало в буфер"
    assert cold_tracker._heat == {}


@pytest.mark.asyncio
async def test_ordinary_channel_is_still_counted(cold_tracker):
    """Опора: трекер вообще считает — иначе предыдущий тест ничего не значит."""
    guild = FakeGuild()
    await cold_tracker.on_message(FakeMessage(guild, FakeChannel(guild, CHANNEL + 1)))
    assert len(cold_tracker._buffer) == 1
    assert len(cold_tracker._heat) == 1


@pytest.mark.asyncio
async def test_bot_commands_are_not_counted(cold_tracker):
    guild = FakeGuild()
    message = FakeMessage(guild, FakeChannel(guild, CHANNEL + 1), content="!top")
    await cold_tracker.on_message(message)
    assert cold_tracker._buffer == {}


# --- поведение при сбоях записи ---


async def counted(db: Database) -> int:
    async with db.conn.execute(
        "SELECT COALESCE(SUM(message_count), 0) AS n FROM message_events_daily"
    ) as cursor:
        return (await cursor.fetchone())["n"]


@pytest_asyncio.fixture
async def tracker(db):
    settings = SettingsStore(db)
    await settings.load()
    await settings.prime([GUILD])
    tracker = TextTracker(FakeBot(make_config(), db, settings))
    yield tracker
    tracker.cog_unload()


async def buffer_one_message(tracker: TextTracker) -> None:
    guild = FakeGuild()
    await tracker.on_message(FakeMessage(guild, FakeChannel(guild)))


@pytest.mark.asyncio
async def test_counters_are_written_once(tracker, db):
    """Опора: без сбоев сообщение доезжает в базу ровно один раз."""
    await buffer_one_message(tracker)
    await tracker.flush()
    assert await counted(db) == 1
    assert tracker._buffer == {}


@pytest.mark.asyncio
async def test_failed_username_update_does_not_double_the_counters(tracker, db, monkeypatch):
    """Счётчики уже записаны — возвращать их в буфер нельзя.

    Обе записи шли в одном try, поэтому сбой при обновлении имён отправлял
    счётчики обратно в буфер, и следующий сброс писал их второй раз.
    """
    async def boom(*args, **kwargs):
        raise RuntimeError("база занята")

    monkeypatch.setattr(db, "upsert_users", boom)
    await buffer_one_message(tracker)
    await tracker.flush()

    assert await counted(db) == 1
    assert tracker._buffer == {}, "счётчики вернулись в буфер и будут записаны дважды"

    monkeypatch.undo()
    await tracker.flush()
    assert await counted(db) == 1


@pytest.mark.asyncio
async def test_failed_counters_return_everything_to_the_buffer(tracker, db, monkeypatch):
    """А вот при сбое самих счётчиков вернуть нужно всё, включая имена."""
    async def boom(*args, **kwargs):
        raise RuntimeError("диск кончился")

    monkeypatch.setattr(db, "add_message_counts", boom)
    await buffer_one_message(tracker)
    await tracker.flush()

    assert await counted(db) == 0
    assert len(tracker._buffer) == 1
    assert len(tracker._heat) == 1
    assert tracker._users, "имена терялись молча"

    monkeypatch.undo()
    await tracker.flush()
    assert await counted(db) == 1


@pytest.mark.asyncio
async def test_returned_counters_merge_with_newly_buffered(tracker, db, monkeypatch):
    """За время неудачной записи могли прийти новые сообщения — их не теряем."""
    async def boom(*args, **kwargs):
        raise RuntimeError("база занята")

    monkeypatch.setattr(db, "add_message_counts", boom)
    await buffer_one_message(tracker)
    await tracker.flush()

    monkeypatch.undo()
    await buffer_one_message(tracker)
    await tracker.flush()
    assert await counted(db) == 2
