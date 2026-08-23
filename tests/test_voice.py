"""Пересборка голосовых сессий под текущие исключения каналов.

`/admin exclude-channel` отвечает «учёт останавливается с этого момента», но
фоновое начисление перебирает уже открытые сессии и канал у них не
перепроверяет. Без пересборки исключённый канал продолжал капать время всем,
кто в нём сидел, — до выхода из канала или перезапуска бота.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.config import Config
from bot.cogs.admin import Admin
from bot.cogs.voice import VoiceTracker
from bot.db import Database
from bot.settings import SettingsStore

UTC = ZoneInfo("UTC")
GUILD, CHANNEL, ALICE = 1, 100, 10
WEIGHTS = {"voice_weight": 1.0, "message_weight": 1.0}


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
        self.voice_channels: list = []
        self.stage_channels: list = []


class FakeChannel:
    def __init__(self, guild: FakeGuild, channel_id: int = CHANNEL) -> None:
        self.id = channel_id
        self.guild = guild
        self.members: list = []


class FakeVoiceState:
    def __init__(self, channel: FakeChannel | None) -> None:
        self.channel = channel
        self.self_stream = False


class FakeMember:
    def __init__(self, guild: FakeGuild, user_id: int = ALICE) -> None:
        self.id = user_id
        self.guild = guild
        self.bot = False
        self.voice: FakeVoiceState | None = None

    def __str__(self) -> str:
        return f"user-{self.id}"


class FakeBot:
    def __init__(self, config, db, settings) -> None:
        self.config = config
        self.db = db
        self.settings = settings
        self.guilds: list = []

    async def wait_until_ready(self) -> None:
        # Фоновые задачи трекера паркуются здесь навсегда: тесты дёргают
        # flush() напрямую, а cog_unload() задачи отменит.
        await asyncio.Event().wait()


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "voice.db"), UTC)
    await database.connect()
    await database.upsert_guild(GUILD, "test")
    await database.upsert_users([(ALICE, "alice", False)])
    yield database
    await database.close()


@pytest_asyncio.fixture
async def scene(db):
    """Трекер с одним участником, сидящим в трекаемом голосовом канале."""
    settings = SettingsStore(db)
    await settings.load()
    await settings.prime([GUILD])

    tracker = VoiceTracker(FakeBot(make_config(), db, settings))
    tracker._reconciled = True

    guild = FakeGuild()
    channel = FakeChannel(guild)
    member = FakeMember(guild)
    member.voice = FakeVoiceState(channel)
    channel.members = [member]
    guild.voice_channels = [channel]

    await tracker.apply_state(member, member.voice)
    yield tracker, settings, guild, channel, member
    tracker.cog_unload()


def age_sessions(tracker: VoiceTracker, minutes: int) -> None:
    """Отмотать сессии назад — как будто прошло время до срабатывания flush."""
    delta = timedelta(minutes=minutes)
    for session in tracker._sessions.values():
        session.joined_at -= delta
        session.credited_until -= delta


async def credited(db) -> int:
    totals = await db.user_totals(GUILD, ALICE, **WEIGHTS)
    return totals.voice_seconds if totals else 0


@pytest.mark.asyncio
async def test_session_opens_for_tracked_channel(scene, db):
    """Опора для остальных тестов: до исключения время исправно капает."""
    tracker, _settings, _guild, _channel, _member = scene
    assert len(tracker._sessions) == 1

    age_sessions(tracker, 1)
    await tracker.flush()
    assert await credited(db) == 60


@pytest.mark.asyncio
async def test_excluding_channel_stops_the_clock(scene, db):
    tracker, settings, guild, channel, _member = scene

    assert await settings.exclude_channel(GUILD, channel.id) is True
    await tracker.resync_guild(guild)
    assert tracker._sessions == {}, "сессия в исключённом канале осталась открытой"

    age_sessions(tracker, 1)
    await tracker.flush()
    assert await credited(db) == 0


@pytest.mark.asyncio
async def test_including_channel_back_starts_the_clock(scene, db):
    """Симметрия: вернувшийся канал должен считаться сразу, а не со следующего захода."""
    tracker, settings, guild, channel, _member = scene

    await settings.exclude_channel(GUILD, channel.id)
    await tracker.resync_guild(guild)
    assert tracker._sessions == {}

    assert await settings.include_channel(GUILD, channel.id) is True
    await tracker.resync_guild(guild)
    assert len(tracker._sessions) == 1

    age_sessions(tracker, 1)
    await tracker.flush()
    assert await credited(db) == 60


@pytest.mark.asyncio
async def test_resync_leaves_other_channels_alone(scene, db):
    """Исключение одного канала не должно ронять сессии в соседнем."""
    tracker, settings, guild, channel, _member = scene

    other = FakeChannel(guild, channel_id=CHANNEL + 1)
    bob = FakeMember(guild, user_id=ALICE + 1)
    bob.voice = FakeVoiceState(other)
    other.members = [bob]
    guild.voice_channels.append(other)
    await tracker.apply_state(bob, bob.voice)
    assert len(tracker._sessions) == 2

    await settings.exclude_channel(GUILD, channel.id)
    await tracker.resync_guild(guild)
    assert list(tracker._sessions) == [(GUILD, bob.id)]


@pytest.mark.asyncio
async def test_resync_before_reconcile_is_a_noop(scene):
    """До первой синхронизации состояние в памяти неполное — трогать нечего."""
    tracker, settings, guild, channel, _member = scene
    tracker._reconciled = False

    await settings.exclude_channel(GUILD, channel.id)
    await tracker.resync_guild(guild)
    assert len(tracker._sessions) == 1


# --- проводка до самой команды ---


class FakePermissions:
    manage_guild = True
    administrator = False


class FakeAdminUser:
    guild_permissions = FakePermissions()


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content, **kwargs):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.user = FakeAdminUser()
        self.response = FakeResponse()


class FakeAdminBot:
    def __init__(self, db, settings, tracker):
        self.db = db
        self.settings = settings
        self.stats = None
        self.config = make_config()
        self._tracker = tracker

    def get_cog(self, name):
        return self._tracker if name == "VoiceTracker" else None


class FakeMentionChannel(FakeChannel):
    @property
    def mention(self):
        return f"<#{self.id}>"


@pytest.mark.asyncio
async def test_exclude_command_stops_the_clock(scene, db):
    """Сквозная проверка: команда обещает остановить учёт — пусть останавливает.

    Идёт через настоящий callback /admin exclude-channel, поэтому на старом
    коде падает по существу: время продолжает капать.
    """
    tracker, settings, guild, channel, _member = scene
    channel.__class__ = FakeMentionChannel

    cog = Admin(FakeAdminBot(db, settings, tracker))
    await Admin.exclude_channel.callback(cog, FakeInteraction(guild), channel)

    age_sessions(tracker, 1)
    await tracker.flush()
    assert await credited(db) == 0, "исключённый канал продолжает начислять время"
