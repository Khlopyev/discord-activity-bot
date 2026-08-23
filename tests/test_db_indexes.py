"""Индексы под агрегатные запросы и статистика планировщика.

Запросы «популярные каналы» и «топ игр» группируют всю таблицу по гильдии.
Индексы (guild_id, date) для этого не годились: строки в них разложены по
датам, поэтому каждая превращалась в случайный доступ к таблице. На базе за
два года «популярные голосовые» отвечали полминуты.

Отдельная беда — выбор индекса. Без статистики планировщик берёт широкий
покрывающий индекс даже там, где есть точный поиск по ключу, и запрос по
одному участнику замедляется на два порядка. Поэтому ANALYZE обязателен.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.db import Database

UTC = ZoneInfo("UTC")
GUILD, ALICE, CHANNEL = 1, 10, 100

EXPECTED_INDEXES = {
    "idx_voice_events_guild_channel",
    "idx_message_events_guild_channel",
    "idx_game_events_guild_name",
    "idx_summary_guild_user",
    # Старые никуда не делись: по ним идут запросы с фильтром по периоду.
    "idx_voice_events_guild_date",
    "idx_game_events_guild_date",
    "idx_summary_guild_date",
}


def dt(day, hour=0):
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


async def fill(db: Database) -> None:
    """Немного данных: на пустых таблицах ANALYZE ничего не соберёт."""
    await db.upsert_guild(GUILD, "test")
    await db.upsert_users([(ALICE, "alice", False), (ALICE + 1, "bot", True)])
    for user_id in (ALICE, ALICE + 1):
        for channel in (CHANNEL, CHANNEL + 1):
            session = await db.open_voice_session(
                guild_id=GUILD, user_id=user_id, channel_id=channel,
                joined_at=dt(17, 10), is_stream=False,
            )
            await db.close_session(session, dt(17, 11), min_session_seconds=10)
    await db.add_message_counts(
        [(GUILD, ALICE, CHANNEL, "2026-08-17", 5, 50)],
        [(GUILD, ALICE, 1, 10, 5)],
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "indexes.db"), UTC)
    await database.connect()
    yield database
    await database.close()


async def plan(db: Database, sql: str, params: tuple = ()) -> str:
    async with db.conn.execute("EXPLAIN QUERY PLAN " + sql, params) as cursor:
        rows = await cursor.fetchall()
    return " | ".join(row[-1] for row in rows)


@pytest.mark.asyncio
async def test_expected_indexes_exist(db):
    async with db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ) as cursor:
        names = {row["name"] for row in await cursor.fetchall()}
    assert EXPECTED_INDEXES <= names


@pytest.mark.asyncio
async def test_statistics_are_collected_once(db):
    await fill(db)
    # Первый connect прошёл на пустой базе, собирать было нечего.
    assert await db.ensure_statistics() is True
    assert await db.ensure_statistics() is False


@pytest.mark.asyncio
async def test_empty_tables_do_not_force_analyze_forever(db):
    """При выключенном трекинге игр game_events_daily пуста всегда.

    Требовать с неё статистику нельзя: ANALYZE по пустой таблице ничего не
    запишет, и сбор запускался бы на каждом старте — на большой базе это
    лишние секунды при каждом запуске.
    """
    await fill(db)  # игровых событий здесь нет
    assert await db.ensure_statistics() is True

    async with db.conn.execute("SELECT COUNT(*) AS n FROM game_events_daily") as cursor:
        assert (await cursor.fetchone())["n"] == 0
    assert await db.ensure_statistics() is False


@pytest.mark.asyncio
async def test_analyze_runs_again_when_a_table_starts_filling(db):
    """База, где статистику собрали до включения трекинга игр, добирает её."""
    await fill(db)
    await db.ensure_statistics()
    assert await db.ensure_statistics() is False

    session = await db.open_presence_session(
        guild_id=GUILD, user_id=ALICE, activity_type="playing",
        activity_name="Dota 2", started_at=dt(17, 10),
    )
    await db.close_presence_session(session, dt(17, 11), min_session_seconds=10)
    assert await db.ensure_statistics() is True


@pytest.mark.asyncio
async def test_channel_totals_read_only_the_index(db):
    """Тяжёлый запрос должен обходиться индексом, не заглядывая в таблицу."""
    await fill(db)
    await db.ensure_statistics()
    shape = await plan(
        db,
        """
        SELECT v.channel_id, SUM(v.voice_seconds) AS voice_seconds
        FROM voice_events_daily v LEFT JOIN users u ON u.user_id = v.user_id
        WHERE v.guild_id = ? AND COALESCE(u.is_bot, 0) = 0
        GROUP BY v.channel_id
        """,
        (GUILD,),
    )
    assert "COVERING INDEX idx_voice_events_guild_channel" in shape


@pytest.mark.asyncio
async def test_per_user_query_still_seeks_by_user(db):
    """Регрессия, которую легко привезти вместе с широким индексом.

    Без статистики планировщик предпочитает покрывающий индекс и вместо
    точного поиска по (guild_id, user_id) вычитывает всю гильдию.
    """
    await fill(db)
    await db.ensure_statistics()
    shape = await plan(
        db,
        """
        SELECT v.channel_id, SUM(v.voice_seconds) AS voice_seconds
        FROM voice_events_daily v
        WHERE v.guild_id = ? AND v.user_id = ?
        GROUP BY v.channel_id
        """,
        (GUILD, ALICE),
    )
    assert "user_id=?" in shape, f"поиск идёт не по участнику: {shape}"


@pytest.mark.asyncio
async def test_results_are_unchanged_by_indexing(db):
    """Индексы не должны менять ответы — боты по-прежнему исключены."""
    await fill(db)
    await db.ensure_statistics()
    channels = await db.top_voice_channels(GUILD)
    assert {channel for channel, _seconds in channels} == {CHANNEL, CHANNEL + 1}
    favourite = await db.user_favorite_voice_channel(GUILD, ALICE)
    assert favourite is not None and favourite[1] == 3600
