"""Выгрузка статистики: чтение кусками и остановка по лимиту вложения.

Раньше /admin export вычитывал все шесть таблиц целиком, собирал архив в
памяти и только потом сверял размер с лимитом. На сервере с годовой историей
это 343 МБ памяти и почти двадцать секунд сжатия — причём прямо в цикле
событий, так что бот всё это время не отвечал Discord. Итог работы при этом
выбрасывался: архив всё равно не проходил по размеру.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.cogs.admin import Admin, ExportTooLarge
from bot.db import Database

UTC = ZoneInfo("UTC")
GUILD, OTHER_GUILD = 1, 2
ALICE = 10


class FakeBot:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.settings = None
        self.stats = None

    def get_cog(self, name):
        return None


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "export.db"), UTC)
    await database.connect()
    await database.upsert_guild(GUILD, "test")
    await database.upsert_users([(ALICE, "alice", False)])
    yield database
    await database.close()


@pytest_asyncio.fixture
async def cog(db):
    return Admin(FakeBot(db))


async def add_messages(db: Database, count: int, guild_id: int = GUILD) -> None:
    await db.add_message_counts(
        [(guild_id, ALICE, 100 + n, f"2026-08-{1 + n % 28:02d}", 1, 10) for n in range(count)]
    )


@pytest.mark.asyncio
async def test_empty_table_still_yields_headers(db):
    chunks = [chunk async for chunk in db.stream_export_rows(GUILD, "message_events_daily")]
    assert len(chunks) == 1
    headers, rows = chunks[0]
    assert rows == []
    assert "message_count" in headers


@pytest.mark.asyncio
async def test_rows_arrive_in_chunks(db):
    await add_messages(db, 5)
    chunks = [
        rows
        async for _headers, rows in db.stream_export_rows(
            GUILD, "message_events_daily", chunk_size=2
        )
    ]
    assert [len(chunk) for chunk in chunks] == [2, 2, 1]


@pytest.mark.asyncio
async def test_stream_is_scoped_to_the_guild(db):
    await db.upsert_guild(OTHER_GUILD, "other")
    await add_messages(db, 3)
    await add_messages(db, 7, guild_id=OTHER_GUILD)
    total = 0
    async for _headers, rows in db.stream_export_rows(GUILD, "message_events_daily"):
        total += len(rows)
    assert total == 3


@pytest.mark.asyncio
async def test_stream_rejects_unknown_table(db):
    with pytest.raises(ValueError, match="не разрешена"):
        async for _ in db.stream_export_rows(GUILD, "users; DROP TABLE users"):
            pass


@pytest.mark.asyncio
async def test_csv_export_is_a_readable_archive(db, cog):
    await add_messages(db, 3)
    blob, total = await cog._collect_export(GUILD, "csv")

    # Шесть, а не три: сообщения ложатся и в message_events_daily, и в
    # дневной агрегат, а total считает строки по всем таблицам сразу.
    assert total == 6
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        text = archive.read("message_events_daily.csv").decode("utf-8")
    assert "message_events_daily.csv" in names
    # Все шесть таблиц попадают в архив даже пустыми — со строкой заголовков.
    assert len(names) == 6
    assert text.startswith("﻿"), "без BOM Excel ломает кириллицу"
    assert len(text.strip().splitlines()) == 1 + 3


@pytest.mark.asyncio
async def test_json_export_is_valid_json(db, cog):
    await add_messages(db, 3)
    blob, total = await cog._collect_export(GUILD, "json")

    parsed = json.loads(blob.decode("utf-8"))
    assert total == 6
    assert set(parsed) == {
        "daily_activity_summary", "message_events_daily", "voice_events_daily",
        "game_events_daily", "activity_heatmap", "user_heatmap",
    }
    assert len(parsed["message_events_daily"]) == 3
    assert parsed["message_events_daily"][0]["guild_id"] == GUILD


@pytest.mark.asyncio
async def test_empty_export_is_still_valid(db, cog):
    """Сервер без статистики не должен ломать формат выгрузки."""
    blob, total = await cog._collect_export(GUILD, "json")
    parsed = json.loads(blob.decode("utf-8"))
    assert total == 0
    assert all(value == [] for value in parsed.values())


@pytest.mark.asyncio
async def test_export_stops_as_soon_as_it_outgrows_the_limit(db, cog, monkeypatch):
    """Сборка обязана прерваться на превышении, а не досчитать и выбросить итог."""
    await add_messages(db, 200)
    monkeypatch.setattr("bot.cogs.admin.MAX_UPLOAD_BYTES", 200)

    with pytest.raises(ExportTooLarge) as overflow:
        await cog._collect_export(GUILD, "json")
    assert overflow.value.rows > 0
