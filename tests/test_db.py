"""Тесты слоя данных — без Discord, на временной SQLite-базе."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.db import Database

UTC = ZoneInfo("UTC")
GUILD = 1
ALICE, BOB, BOT_USER = 10, 20, 99


def dt(day, hour=0, minute=0):
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"), UTC)
    await database.connect()
    await database.upsert_guild(GUILD, "test")
    await database.upsert_users(
        [(ALICE, "alice", False), (BOB, "bob", False), (BOT_USER, "some-bot", True)]
    )
    yield database
    await database.close()


async def voice(db, user_id, start, end, *, is_stream=False, min_seconds=10):
    session = await db.open_voice_session(
        guild_id=GUILD, user_id=user_id, channel_id=100, joined_at=start, is_stream=is_stream
    )
    await db.close_session(session, end, min_session_seconds=min_seconds)
    return session


@pytest.mark.asyncio
async def test_voice_time_lands_in_daily_summary(db):
    await voice(db, ALICE, dt(17, 10), dt(17, 12))
    totals = await db.user_totals(GUILD, ALICE, voice_weight=1.0, message_weight=1.0)
    assert totals.voice_seconds == 7200
    assert totals.stream_seconds == 0


@pytest.mark.asyncio
async def test_session_crossing_midnight_splits_across_two_days(db):
    await voice(db, ALICE, dt(17, 23, 30), dt(18, 0, 30))
    async with db.conn.execute(
        "SELECT date, voice_seconds FROM daily_activity_summary WHERE user_id = ? ORDER BY date",
        (ALICE,),
    ) as cursor:
        rows = await cursor.fetchall()
    assert [(r["date"], r["voice_seconds"]) for r in rows] == [
        ("2026-08-17", 1800),
        ("2026-08-18", 1800),
    ]


@pytest.mark.asyncio
async def test_stream_time_counted_separately(db):
    await voice(db, ALICE, dt(17, 10), dt(17, 11), is_stream=True)
    totals = await db.user_totals(GUILD, ALICE, voice_weight=1.0, message_weight=1.0)
    assert totals.voice_seconds == 3600
    assert totals.stream_seconds == 3600


@pytest.mark.asyncio
async def test_phantom_session_is_discarded(db):
    start = dt(17, 10)
    await voice(db, ALICE, start, start + timedelta(seconds=4), min_seconds=10)

    totals = await db.user_totals(GUILD, ALICE, voice_weight=1.0, message_weight=1.0)
    assert totals is None
    async with db.conn.execute("SELECT COUNT(*) AS n FROM voice_sessions") as cursor:
        assert (await cursor.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_incremental_crediting_is_not_double_counted(db):
    session = await db.open_voice_session(
        guild_id=GUILD, user_id=ALICE, channel_id=100, joined_at=dt(17, 10), is_stream=False
    )
    await db.credit_session(session, dt(17, 10, 30), min_session_seconds=10)
    await db.credit_session(session, dt(17, 11), min_session_seconds=10)
    await db.close_session(session, dt(17, 11, 15), min_session_seconds=10)

    totals = await db.user_totals(GUILD, ALICE, voice_weight=1.0, message_weight=1.0)
    assert totals.voice_seconds == 4500


@pytest.mark.asyncio
async def test_orphaned_session_credits_only_confirmed_time(db):
    session = await db.open_voice_session(
        guild_id=GUILD, user_id=ALICE, channel_id=100, joined_at=dt(17, 10), is_stream=False
    )
    await db.credit_session(session, dt(17, 10, 30), min_session_seconds=10)
    # Бот "падает": сессия остаётся открытой, простой не начисляется.
    assert await db.close_orphaned_sessions(min_session_seconds=10) == 1

    totals = await db.user_totals(GUILD, ALICE, voice_weight=1.0, message_weight=1.0)
    assert totals.voice_seconds == 1800
    assert await db.fetch_open_sessions() == []


@pytest.mark.asyncio
async def test_message_counts_aggregate_per_channel_and_summary(db):
    await db.add_message_counts(
        [
            (GUILD, ALICE, 100, "2026-08-17", 3, 30),
            (GUILD, ALICE, 200, "2026-08-17", 2, 20),
            (GUILD, ALICE, 100, "2026-08-17", 1, 10),
        ]
    )
    async with db.conn.execute(
        "SELECT channel_id, message_count, char_count_approx FROM message_events_daily"
        " WHERE user_id = ? ORDER BY channel_id",
        (ALICE,),
    ) as cursor:
        rows = await cursor.fetchall()
    assert [(r["channel_id"], r["message_count"], r["char_count_approx"]) for r in rows] == [
        (100, 4, 40),
        (200, 2, 20),
    ]

    totals = await db.user_totals(GUILD, ALICE, voice_weight=1.0, message_weight=1.0)
    assert totals.message_count == 6


@pytest.mark.asyncio
async def test_leaderboard_ordering_per_metric(db):
    await voice(db, ALICE, dt(17, 10), dt(17, 12))          # 120 минут
    await voice(db, BOB, dt(17, 10), dt(17, 10, 30))        # 30 минут
    await db.add_message_counts([(GUILD, BOB, 100, "2026-08-17", 200, 2000)])
    await db.add_message_counts([(GUILD, ALICE, 100, "2026-08-17", 5, 50)])

    weights = {"voice_weight": 1.0, "message_weight": 2.0}
    by_voice = await db.leaderboard(GUILD, "voice", limit=10, **weights)
    assert [r.user_id for r in by_voice] == [ALICE, BOB]

    by_text = await db.leaderboard(GUILD, "text", limit=10, **weights)
    assert [r.user_id for r in by_text] == [BOB, ALICE]

    # combined: alice 120*1 + 5*2 = 130; bob 30*1 + 200*2 = 430
    by_combined = await db.leaderboard(GUILD, "combined", limit=10, **weights)
    assert [r.user_id for r in by_combined] == [BOB, ALICE]
    assert by_combined[0].combined_score == pytest.approx(430.0)


@pytest.mark.asyncio
async def test_bots_excluded_from_leaderboard(db):
    await voice(db, BOT_USER, dt(17, 10), dt(17, 12))
    rows = await db.leaderboard(GUILD, "voice", limit=10, voice_weight=1.0, message_weight=1.0)
    assert rows == []


@pytest.mark.asyncio
async def test_user_rank(db):
    await voice(db, ALICE, dt(17, 10), dt(17, 12))
    await voice(db, BOB, dt(17, 10), dt(17, 10, 30))

    weights = {"voice_weight": 1.0, "message_weight": 1.0}
    assert await db.user_rank(GUILD, ALICE, **weights) == (1, 2)
    assert await db.user_rank(GUILD, BOB, **weights) == (2, 2)
    assert await db.user_rank(GUILD, 12345, **weights) is None


@pytest.mark.asyncio
async def test_leaderboard_period_filter(db):
    await voice(db, ALICE, dt(10, 10), dt(10, 12))   # старое
    await voice(db, BOB, dt(18, 10), dt(18, 10, 30))  # свежее
    weights = {"voice_weight": 1.0, "message_weight": 1.0}

    all_time = await db.leaderboard(GUILD, "voice", limit=10, **weights)
    assert [r.user_id for r in all_time] == [ALICE, BOB]

    recent = await db.leaderboard(GUILD, "voice", limit=10, since="2026-08-17", **weights)
    assert [r.user_id for r in recent] == [BOB]


@pytest.mark.asyncio
async def test_user_totals_and_rank_respect_period(db):
    await voice(db, ALICE, dt(10, 10), dt(10, 12))
    await voice(db, ALICE, dt(18, 10), dt(18, 10, 15))
    await voice(db, BOB, dt(18, 10), dt(18, 11))
    weights = {"voice_weight": 1.0, "message_weight": 1.0}

    assert (await db.user_totals(GUILD, ALICE, **weights)).voice_seconds == 8100
    scoped = await db.user_totals(GUILD, ALICE, since="2026-08-17", **weights)
    assert scoped.voice_seconds == 900

    # За всё время Алиса первая, но за свежий период впереди Боб.
    assert await db.user_rank(GUILD, ALICE, **weights) == (1, 2)
    assert await db.user_rank(GUILD, ALICE, since="2026-08-17", **weights) == (2, 2)


@pytest.mark.asyncio
async def test_guild_totals(db):
    await voice(db, ALICE, dt(17, 10), dt(17, 11), is_stream=True)
    await voice(db, BOB, dt(18, 10), dt(18, 11))
    await db.add_message_counts([(GUILD, ALICE, 100, "2026-08-17", 7, 70)])

    totals = await db.guild_totals(GUILD)
    assert totals.voice_seconds == 7200
    assert totals.stream_seconds == 3600
    assert totals.message_count == 7
    assert totals.active_users == 2
    assert totals.active_days == 2

    scoped = await db.guild_totals(GUILD, since="2026-08-18")
    assert scoped.voice_seconds == 3600
    assert scoped.active_users == 1


@pytest.mark.asyncio
async def test_top_channels(db):
    for channel, minutes in ((111, 60), (222, 20)):
        session = await db.open_voice_session(
            guild_id=GUILD, user_id=ALICE, channel_id=channel,
            joined_at=dt(17, 10), is_stream=False,
        )
        await db.close_session(
            session, dt(17, 10) + timedelta(minutes=minutes), min_session_seconds=10
        )
    await db.add_message_counts(
        [(GUILD, ALICE, 300, "2026-08-17", 5, 50), (GUILD, BOB, 400, "2026-08-17", 9, 90)]
    )

    assert await db.top_voice_channels(GUILD) == [(111, 3600), (222, 1200)]
    assert await db.top_text_channels(GUILD) == [(400, 9), (300, 5)]


@pytest.mark.asyncio
async def test_bots_excluded_from_channel_stats(db):
    await voice(db, BOT_USER, dt(17, 10), dt(17, 12))
    assert await db.top_voice_channels(GUILD) == []


@pytest.mark.asyncio
async def test_heatmap_filled_from_voice_and_messages(db):
    # 2026-08-17 — понедельник, в нотации SQLite это 1.
    await voice(db, ALICE, dt(17, 10, 30), dt(17, 11, 30))
    await db.add_message_counts(
        [(GUILD, BOB, 100, "2026-08-17", 4, 40)], [(GUILD, BOB, 1, 21, 4)]
    )

    async with db.conn.execute(
        "SELECT weekday, hour, voice_seconds, message_count FROM activity_heatmap"
        " ORDER BY hour"
    ) as cursor:
        rows = await cursor.fetchall()
    assert [tuple(r) for r in rows] == [(1, 10, 1800, 0), (1, 11, 1800, 0), (1, 21, 0, 4)]

    weights = {"voice_weight": 1.0, "message_weight": 1.0}
    assert await db.busiest_weekday(GUILD, **weights) == 1
    # Час 10 и 11 дают по 30 очков, час 21 — только 4, значит пик в войсе.
    assert await db.busiest_hour(GUILD, **weights) in (10, 11)


@pytest.mark.asyncio
async def test_user_heatmap_separates_participants(db):
    await voice(db, ALICE, dt(17, 10, 30), dt(17, 11, 30))
    await db.add_message_counts(
        [(GUILD, BOB, 100, "2026-08-17", 4, 40)], [(GUILD, BOB, 1, 21, 4)]
    )
    weights = {"voice_weight": 1.0, "message_weight": 1.0}

    # У каждого свой пик, а серверный считается по сумме.
    assert await db.busiest_hour(GUILD, user_id=ALICE, **weights) in (10, 11)
    assert await db.busiest_hour(GUILD, user_id=BOB, **weights) == 21
    assert await db.busiest_slot(GUILD, user_id=BOB, **weights) == (1, 21)
    assert await db.busiest_hour(GUILD, user_id=12345, **weights) is None


@pytest.mark.asyncio
async def test_heatmap_grid_shapes(db):
    await voice(db, ALICE, dt(17, 10, 30), dt(17, 11, 30))
    await db.add_message_counts(
        [(GUILD, BOB, 100, "2026-08-17", 4, 40)], [(GUILD, BOB, 1, 21, 4)]
    )
    weights = {"voice_weight": 1.0, "message_weight": 1.0}

    guild_grid = await db.heatmap_grid(GUILD, **weights)
    assert set(guild_grid) == {(1, 10), (1, 11), (1, 21)}
    assert guild_grid[(1, 10)] == pytest.approx(30.0)

    alice_grid = await db.heatmap_grid(GUILD, user_id=ALICE, **weights)
    assert set(alice_grid) == {(1, 10), (1, 11)}
    assert await db.heatmap_grid(GUILD, user_id=12345, **weights) == {}


@pytest.mark.asyncio
async def test_purge_user_clears_personal_heatmap(db):
    await db.add_message_counts(
        [(GUILD, ALICE, 100, "2026-08-17", 4, 40)], [(GUILD, ALICE, 1, 21, 4)]
    )
    await db.purge_user(GUILD, ALICE)

    weights = {"voice_weight": 1.0, "message_weight": 1.0}
    assert await db.heatmap_grid(GUILD, user_id=ALICE, **weights) == {}


@pytest.mark.asyncio
async def test_user_daily_series_returns_only_existing_days(db):
    await voice(db, ALICE, dt(17, 10), dt(17, 11))
    await voice(db, ALICE, dt(19, 10), dt(19, 10, 30))
    await db.add_message_counts([(GUILD, ALICE, 100, "2026-08-17", 10, 100)])

    series = await db.user_daily_series(
        GUILD, ALICE, since="2026-08-16", voice_weight=1.0, message_weight=2.0
    )
    assert sorted(series) == ["2026-08-17", "2026-08-19"]
    # 60 минут * 1 + 10 сообщений * 2
    assert series["2026-08-17"].score == pytest.approx(80.0)
    assert series["2026-08-19"].voice_seconds == 1800

    scoped = await db.user_daily_series(
        GUILD, ALICE, since="2026-08-18", voice_weight=1.0, message_weight=2.0
    )
    assert sorted(scoped) == ["2026-08-19"]


@pytest.mark.asyncio
async def test_user_favorite_voice_channel(db):
    for channel, minutes in ((111, 20), (222, 90)):
        session = await db.open_voice_session(
            guild_id=GUILD, user_id=ALICE, channel_id=channel,
            joined_at=dt(17, 10), is_stream=False,
        )
        await db.close_session(
            session, dt(17, 10) + timedelta(minutes=minutes), min_session_seconds=10
        )

    assert await db.user_favorite_voice_channel(GUILD, ALICE) == (222, 5400)
    assert await db.user_favorite_voice_channel(GUILD, BOB) is None


async def played(db, user_id, name, start, end, *, min_seconds=10):
    session = await db.open_presence_session(
        guild_id=GUILD, user_id=user_id, activity_type="playing",
        activity_name=name, started_at=start,
    )
    await db.close_presence_session(session, end, min_session_seconds=min_seconds)
    return session


@pytest.mark.asyncio
async def test_top_games_and_user_top_game(db):
    await played(db, ALICE, "Dota 2", dt(17, 10), dt(17, 12))
    await played(db, ALICE, "Valorant", dt(17, 13), dt(17, 13, 30))
    await played(db, BOB, "Dota 2", dt(18, 10), dt(18, 11))

    top = await db.top_games(GUILD)
    assert [(g.name, g.seconds, g.players) for g in top] == [
        ("Dota 2", 10800, 2),
        ("Valorant", 1800, 1),
    ]

    assert (await db.user_top_game(GUILD, ALICE)).name == "Dota 2"
    assert await db.user_top_game(GUILD, 12345) is None

    scoped = await db.top_games(GUILD, since="2026-08-18")
    assert [(g.name, g.seconds) for g in scoped] == [("Dota 2", 3600)]


@pytest.mark.asyncio
async def test_presence_session_crossing_midnight_splits(db):
    await played(db, ALICE, "Dota 2", dt(17, 23, 30), dt(18, 0, 30))
    async with db.conn.execute(
        "SELECT date, seconds FROM game_events_daily ORDER BY date"
    ) as cursor:
        rows = await cursor.fetchall()
    assert [(r["date"], r["seconds"]) for r in rows] == [
        ("2026-08-17", 1800),
        ("2026-08-18", 1800),
    ]


@pytest.mark.asyncio
async def test_short_presence_session_discarded(db):
    await played(db, ALICE, "Dota 2", dt(17, 10), dt(17, 10) + timedelta(seconds=4))
    assert await db.top_games(GUILD) == []
    async with db.conn.execute("SELECT COUNT(*) AS n FROM presence_sessions") as cursor:
        assert (await cursor.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_orphaned_presence_credits_only_confirmed_time(db):
    session = await db.open_presence_session(
        guild_id=GUILD, user_id=ALICE, activity_type="playing",
        activity_name="Dota 2", started_at=dt(17, 10),
    )
    await db.credit_presence(session, dt(17, 10, 30), min_session_seconds=10)
    assert await db.close_orphaned_presence_sessions(min_session_seconds=10) == 1

    top = await db.top_games(GUILD)
    assert [(g.name, g.seconds) for g in top] == [("Dota 2", 1800)]


@pytest.mark.asyncio
async def test_bots_excluded_from_top_games(db):
    await played(db, BOT_USER, "Dota 2", dt(17, 10), dt(17, 12))
    assert await db.top_games(GUILD) == []


@pytest.mark.asyncio
async def test_prune_presence_keeps_game_aggregates(db):
    await played(db, ALICE, "Dota 2", dt(1, 10), dt(1, 12))
    assert await db.prune_raw_presence(dt(20)) == 1

    async with db.conn.execute("SELECT COUNT(*) AS n FROM presence_sessions") as cursor:
        assert (await cursor.fetchone())["n"] == 0
    assert (await db.top_games(GUILD))[0].seconds == 7200


@pytest.mark.asyncio
async def test_empty_guild_stats_are_safe(db):
    weights = {"voice_weight": 1.0, "message_weight": 1.0}
    totals = await db.guild_totals(GUILD)
    assert totals.voice_seconds == 0 and totals.active_users == 0
    assert await db.busiest_weekday(GUILD, **weights) is None
    assert await db.busiest_hour(GUILD, **weights) is None
    assert await db.top_voice_channels(GUILD) == []


@pytest.mark.asyncio
async def test_prune_keeps_aggregates(db):
    await voice(db, ALICE, dt(1, 10), dt(1, 12))
    deleted = await db.prune_raw_sessions(dt(20))
    assert deleted == 1

    async with db.conn.execute("SELECT COUNT(*) AS n FROM voice_sessions") as cursor:
        assert (await cursor.fetchone())["n"] == 0
    totals = await db.user_totals(GUILD, ALICE, voice_weight=1.0, message_weight=1.0)
    assert totals.voice_seconds == 7200
