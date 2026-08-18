"""Тесты границ периодов для автосводок и фильтрации по закрытому периоду."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.cogs.summaries import previous_month, previous_week
from bot.db import Database

UTC = ZoneInfo("UTC")
GUILD = 1
ALICE = 10


def dt(month, day, hour=0):
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "summary.db"), UTC)
    await database.connect()
    await database.upsert_guild(GUILD, "test")
    await database.upsert_users([(ALICE, "alice", False)])
    yield database
    await database.close()


def test_previous_week_from_midweek():
    # 19 августа 2026 — среда.
    assert previous_week(date(2026, 8, 19)) == (date(2026, 8, 10), date(2026, 8, 16))


def test_previous_week_from_monday():
    # В понедельник сводка должна охватывать только что закрывшуюся неделю.
    assert previous_week(date(2026, 8, 17)) == (date(2026, 8, 10), date(2026, 8, 16))


def test_previous_week_from_sunday():
    assert previous_week(date(2026, 8, 23)) == (date(2026, 8, 10), date(2026, 8, 16))


def test_previous_month():
    assert previous_month(date(2026, 8, 5)) == (date(2026, 7, 1), date(2026, 7, 31))


def test_previous_month_across_year_boundary():
    assert previous_month(date(2026, 1, 1)) == (date(2025, 12, 1), date(2025, 12, 31))


def test_previous_month_from_february():
    assert previous_month(date(2026, 3, 1)) == (date(2026, 2, 1), date(2026, 2, 28))


@pytest.mark.asyncio
async def test_closed_period_excludes_neighbouring_days(db):
    for day in (9, 10, 16, 17):
        session = await db.open_voice_session(
            guild_id=GUILD, user_id=ALICE, channel_id=5,
            joined_at=dt(8, day, 10), is_stream=False,
        )
        await db.close_session(session, dt(8, day, 11), min_session_seconds=10)

    # Прошлая неделя: 10–16 августа. Дни 9 и 17 в неё попасть не должны.
    totals = await db.guild_totals(GUILD, since="2026-08-10", until="2026-08-16")
    assert totals.voice_seconds == 7200
    assert totals.active_days == 2


@pytest.mark.asyncio
async def test_leaderboard_respects_upper_bound(db):
    for day, minutes in ((10, 60), (17, 120)):
        session = await db.open_voice_session(
            guild_id=GUILD, user_id=ALICE, channel_id=5,
            joined_at=dt(8, day, 10), is_stream=False,
        )
        await db.close_session(
            session, dt(8, day, 10 + minutes // 60), min_session_seconds=10
        )

    weights = {"voice_weight": 1.0, "message_weight": 1.0}
    rows = await db.leaderboard(
        GUILD, "voice", limit=10, since="2026-08-10", until="2026-08-16", **weights
    )
    assert rows[0].voice_seconds == 3600


@pytest.mark.asyncio
async def test_top_games_respects_upper_bound(db):
    for day in (10, 17):
        session = await db.open_presence_session(
            guild_id=GUILD, user_id=ALICE, activity_type="playing",
            activity_name="Dota 2", started_at=dt(8, day, 10),
        )
        await db.close_presence_session(session, dt(8, day, 11), min_session_seconds=10)

    games = await db.top_games(GUILD, since="2026-08-10", until="2026-08-16")
    assert [(g.name, g.seconds) for g in games] == [("Dota 2", 3600)]
