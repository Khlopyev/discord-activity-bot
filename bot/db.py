"""Слой доступа к данным (SQLite через aiosqlite)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import aiosqlite

from .timeutil import ISO_DATE, from_iso, split_into_hours, sqlite_weekday, to_iso, utcnow

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Метрики лидерборда -> выражение сортировки. Белый список: значение
# подставляется в SQL напрямую, поэтому оно не должно приходить от пользователя.
METRIC_ORDER = {
    "voice": "voice_seconds",
    "text": "message_count",
    "combined": "combined_score",
}

# Индексы, ради которых собирается статистика планировщика: пока её нет,
# он предпочитает их точному поиску по ключу там, где точный поиск возможен.
# Рядом — таблица индекса: по пустой таблице ANALYZE ничего не запишет, и
# требовать с неё статистику бессмысленно.
ANALYZED_INDEXES = {
    "idx_voice_events_guild_channel": "voice_events_daily",
    "idx_message_events_guild_channel": "message_events_daily",
    "idx_game_events_guild_name": "game_events_daily",
    "idx_summary_guild_user": "daily_activity_summary",
}

# Белый список таблиц для /admin export — имя подставляется в SQL напрямую.
EXPORTABLE_TABLES = (
    "daily_activity_summary",
    "message_events_daily",
    "voice_events_daily",
    "game_events_daily",
    "activity_heatmap",
    "user_heatmap",
)


@dataclass
class OpenSession:
    """Открытая голосовая сессия (сегмент с неизменным каналом и флагом стрима)."""

    id: int
    guild_id: int
    user_id: int
    channel_id: int
    joined_at: datetime
    credited_until: datetime
    is_stream: bool


@dataclass
class OpenPresence:
    """Открытая сессия Rich Presence — отрезок с неизменным названием игры."""

    id: int
    guild_id: int
    user_id: int
    activity_type: str
    activity_name: str
    started_at: datetime
    credited_until: datetime


@dataclass
class GameRow:
    name: str
    seconds: int
    players: int


@dataclass
class DailyPoint:
    date: str
    voice_seconds: int
    message_count: int
    score: float


@dataclass
class GuildTotals:
    voice_seconds: int
    message_count: int
    stream_seconds: int
    active_users: int
    active_days: int


@dataclass
class LeaderboardRow:
    user_id: int
    username: str | None
    voice_seconds: int
    message_count: int
    stream_seconds: int
    combined_score: float


class Database:
    def __init__(self, path: str, tz: ZoneInfo) -> None:
        self._path = Path(path)
        self._tz = tz
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # --- жизненный цикл ---

    async def connect(self) -> None:
        if self._path.parent and str(self._path.parent) not in ("", "."):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._conn.commit()
        await self.ensure_statistics()
        log.info("БД готова: %s", self._path.resolve())

    async def ensure_statistics(self) -> bool:
        """Собрать статистику планировщика, если её ещё нет. True — если собрали.

        Без sqlite_stat1 планировщик выбирает индекс по форме запроса и на
        покрывающих индексах ошибается: для запроса, отфильтрованного по
        участнику, он берёт широкий индекс вместо точного поиска по ключу и
        замедляется на два порядка. ANALYZE эту ошибку снимает.

        Проверяем не наличие строк вообще, а строки по нужным индексам: ANALYZE
        на пустой базе записывает несколько строк для частичных индексов
        voice_sessions, и по ним всё выглядело бы уже собранным. Пока
        агрегатные таблицы пусты, собирать нечего — попробуем на следующем
        запуске; как только данные появятся, статистика соберётся и осядет.

        Дальше её освежает PRAGMA optimize при закрытии.
        """
        analysed = await self._analysed_indexes()
        missing = [
            table for index, table in ANALYZED_INDEXES.items() if index not in analysed
        ]
        if not any([await self._has_rows(table) for table in missing]):
            return False

        log.info("Собираю статистику планировщика (разово, может занять секунды)")
        await self.conn.execute("ANALYZE")
        await self.conn.commit()
        return True

    async def _analysed_indexes(self) -> set[str]:
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_stat1'"
        ) as cursor:
            if await cursor.fetchone() is None:
                return set()
        async with self.conn.execute("SELECT DISTINCT idx FROM sqlite_stat1") as cursor:
            return {row["idx"] for row in await cursor.fetchall() if row["idx"]}

    async def _has_rows(self, table: str) -> bool:
        # Имя таблицы — из константы модуля, не из пользовательского ввода.
        async with self.conn.execute(f"SELECT EXISTS(SELECT 1 FROM {table}) AS any_row") as c:
            return bool((await c.fetchone())["any_row"])

    async def close(self) -> None:
        if self._conn is not None:
            try:
                # Рекомендованный SQLite способ держать статистику свежей:
                # пересчитывает только то, что заметно изменилось. Лимит
                # ограничивает работу, чтобы остановка не затягивалась.
                await self._conn.execute("PRAGMA analysis_limit=400")
                await self._conn.execute("PRAGMA optimize")
                await self._conn.commit()
            except Exception:
                # Остановка бота не должна падать из-за обслуживания базы.
                log.exception("Не удалось обновить статистику планировщика")
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не был вызван")
        return self._conn

    # --- справочники ---

    async def upsert_guild(self, guild_id: int, name: str | None) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO guilds (guild_id, name, created_at) VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name
                """,
                (guild_id, name, to_iso(utcnow())),
            )
            await self.conn.commit()

    async def get_guild_settings(self, guild_id: int) -> dict:
        async with self.conn.execute(
            "SELECT settings_json FROM guilds WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["settings_json"]) or {}
        except json.JSONDecodeError:
            log.warning("Повреждён settings_json гильдии %s, читаю как пустой", guild_id)
            return {}

    async def update_guild_settings(self, guild_id: int, patch: dict) -> dict:
        """Слить патч с текущими настройками и вернуть результат."""
        async with self._lock:
            async with self.conn.execute(
                "SELECT settings_json FROM guilds WHERE guild_id = ?", (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()
            try:
                current = json.loads(row["settings_json"]) if row else {}
            except (json.JSONDecodeError, TypeError):
                current = {}
            current.update(patch)
            await self.conn.execute(
                """
                INSERT INTO guilds (guild_id, settings_json, created_at) VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET settings_json = excluded.settings_json
                """,
                (guild_id, json.dumps(current, ensure_ascii=False), to_iso(utcnow())),
            )
            await self.conn.commit()
        return current

    # --- отказ от трекинга (раздел 7) ---

    async def fetch_optouts(self) -> set[tuple[int, int]]:
        async with self.conn.execute("SELECT guild_id, user_id FROM optouts") as cursor:
            rows = await cursor.fetchall()
        return {(row["guild_id"], row["user_id"]) for row in rows}

    async def set_optout(self, guild_id: int, user_id: int, opted_out: bool) -> None:
        async with self._lock:
            if opted_out:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO optouts (guild_id, user_id, created_at)"
                    " VALUES (?, ?, ?)",
                    (guild_id, user_id, to_iso(utcnow())),
                )
            else:
                await self.conn.execute(
                    "DELETE FROM optouts WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            await self.conn.commit()

    async def purge_user(self, guild_id: int, user_id: int) -> int:
        """Удалить всю статистику участника на сервере. Возвращает число строк."""
        tables = (
            "daily_activity_summary",
            "message_events_daily",
            "voice_events_daily",
            "game_events_daily",
            "user_heatmap",
            "voice_sessions",
            "presence_sessions",
        )
        removed = 0
        async with self._lock:
            for table in tables:
                cursor = await self.conn.execute(
                    f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
                removed += cursor.rowcount or 0
            await self.conn.commit()
        return removed

    async def purge_guild(self, guild_id: int) -> int:
        """Полный сброс статистики сервера. Настройки и отказы сохраняются."""
        tables = (
            "daily_activity_summary",
            "message_events_daily",
            "voice_events_daily",
            "game_events_daily",
            "activity_heatmap",
            "user_heatmap",
            "voice_sessions",
            "presence_sessions",
        )
        removed = 0
        async with self._lock:
            for table in tables:
                cursor = await self.conn.execute(
                    f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,)
                )
                removed += cursor.rowcount or 0
            await self.conn.commit()
        return removed

    async def stream_export_rows(
        self, guild_id: int, table: str, *, chunk_size: int = 20_000
    ):
        """Та же выгрузка, но кусками: (заголовки, порция строк).

        Читать таблицу целиком нельзя — на сервере с двухлетней историей это
        сотни мегабайт в памяти процесса. Для пустой таблицы отдаётся один
        кусок с заголовками и без строк, чтобы вызывающему не нужно было
        отдельно спрашивать структуру.
        """
        if table not in EXPORTABLE_TABLES:
            raise ValueError(f"Таблица {table!r} не разрешена к выгрузке")
        async with self.conn.execute(
            f"SELECT * FROM {table} WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            headers = [column[0] for column in cursor.description]
            empty = True
            while True:
                rows = await cursor.fetchmany(chunk_size)
                if not rows:
                    if empty:
                        yield headers, []
                    return
                empty = False
                yield headers, [tuple(row) for row in rows]

    async def upsert_users(self, users: Iterable[tuple[int, str | None, bool]]) -> None:
        rows = [(uid, name, int(is_bot), to_iso(utcnow())) for uid, name, is_bot in users]
        if not rows:
            return
        async with self._lock:
            await self.conn.executemany(
                """
                INSERT INTO users (user_id, username, is_bot, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    is_bot = excluded.is_bot,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            await self.conn.commit()

    # --- голосовые сессии ---

    async def open_voice_session(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        joined_at: datetime,
        is_stream: bool,
    ) -> OpenSession:
        stamp = to_iso(joined_at)
        async with self._lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO voice_sessions
                    (guild_id, user_id, channel_id, joined_at, is_stream, is_open, credited_until)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (guild_id, user_id, channel_id, stamp, int(is_stream), stamp),
            )
            await self.conn.commit()
            session_id = cursor.lastrowid
        return OpenSession(
            id=int(session_id),
            guild_id=guild_id,
            user_id=user_id,
            channel_id=channel_id,
            joined_at=joined_at,
            credited_until=joined_at,
            is_stream=is_stream,
        )

    async def fetch_open_sessions(self) -> list[OpenSession]:
        async with self.conn.execute(
            "SELECT * FROM voice_sessions WHERE is_open = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            OpenSession(
                id=row["id"],
                guild_id=row["guild_id"],
                user_id=row["user_id"],
                channel_id=row["channel_id"],
                joined_at=from_iso(row["joined_at"]),
                credited_until=from_iso(row["credited_until"]),
                is_stream=bool(row["is_stream"]),
            )
            for row in rows
        ]

    async def credit_session(
        self, session: OpenSession, until: datetime, *, min_session_seconds: int
    ) -> None:
        """Начислить время сессии в дневной агрегат до момента `until`.

        Пока сессия короче `min_session_seconds`, не начисляем ничего — так
        «фантомные» сессии (раздел 8 брифа) удаляются при закрытии бесследно.
        """
        if (until - session.joined_at).total_seconds() < min_session_seconds:
            return
        chunks = split_into_hours(session.credited_until, until, self._tz)
        if not chunks:
            return

        # Часовые куски сворачиваем в три агрегата: посуточный, поканальный
        # и тепловую карту.
        by_day: dict[str, int] = {}
        by_slot: dict[tuple[int, int], int] = {}
        for day, hour, seconds in chunks:
            key = day.strftime(ISO_DATE)
            by_day[key] = by_day.get(key, 0) + seconds
            slot = (sqlite_weekday(day), hour)
            by_slot[slot] = by_slot.get(slot, 0) + seconds

        stream = session.is_stream
        duration = int(round((until - session.joined_at).total_seconds()))
        async with self._lock:
            for day_key, seconds in by_day.items():
                await self.conn.execute(
                    """
                    INSERT INTO daily_activity_summary
                        (guild_id, user_id, date, voice_seconds, message_count, stream_seconds)
                    VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                        voice_seconds = voice_seconds + excluded.voice_seconds,
                        stream_seconds = stream_seconds + excluded.stream_seconds
                    """,
                    (
                        session.guild_id,
                        session.user_id,
                        day_key,
                        seconds,
                        seconds if stream else 0,
                    ),
                )
                await self.conn.execute(
                    """
                    INSERT INTO voice_events_daily
                        (guild_id, user_id, channel_id, date, voice_seconds, stream_seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, channel_id, date) DO UPDATE SET
                        voice_seconds = voice_seconds + excluded.voice_seconds,
                        stream_seconds = stream_seconds + excluded.stream_seconds
                    """,
                    (
                        session.guild_id,
                        session.user_id,
                        session.channel_id,
                        day_key,
                        seconds,
                        seconds if stream else 0,
                    ),
                )
            for (weekday, hour), seconds in by_slot.items():
                await self.conn.execute(
                    """
                    INSERT INTO activity_heatmap (guild_id, weekday, hour, voice_seconds, message_count)
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(guild_id, weekday, hour) DO UPDATE SET
                        voice_seconds = voice_seconds + excluded.voice_seconds
                    """,
                    (session.guild_id, weekday, hour, seconds),
                )
                await self.conn.execute(
                    """
                    INSERT INTO user_heatmap
                        (guild_id, user_id, weekday, hour, voice_seconds, message_count)
                    VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(guild_id, user_id, weekday, hour) DO UPDATE SET
                        voice_seconds = voice_seconds + excluded.voice_seconds
                    """,
                    (session.guild_id, session.user_id, weekday, hour, seconds),
                )
            await self.conn.execute(
                "UPDATE voice_sessions SET credited_until = ?, duration_seconds = ? WHERE id = ?",
                (to_iso(until), duration, session.id),
            )
            await self.conn.commit()
        session.credited_until = until

    async def close_session(
        self, session: OpenSession, left_at: datetime, *, min_session_seconds: int
    ) -> None:
        await self.credit_session(session, left_at, min_session_seconds=min_session_seconds)
        duration = int(round((left_at - session.joined_at).total_seconds()))
        async with self._lock:
            if duration < min_session_seconds:
                # Ничего не было начислено (см. credit_session) — удаляем бесследно.
                await self.conn.execute("DELETE FROM voice_sessions WHERE id = ?", (session.id,))
            else:
                await self.conn.execute(
                    "UPDATE voice_sessions SET is_open = 0, left_at = ?, duration_seconds = ? WHERE id = ?",
                    (to_iso(left_at), duration, session.id),
                )
            await self.conn.commit()

    async def close_orphaned_sessions(self, *, min_session_seconds: int) -> int:
        """Закрыть сессии, оставшиеся открытыми после падения/перезапуска.

        Время простоя бота не начисляется: сессия закрывается моментом
        `credited_until`, то есть последним подтверждённым начислением.
        """
        async with self._lock:
            # Сначала удаляем короткие — но только среди осиротевших. Раньше
            # удаление шло по всей таблице после закрытия, и поднятый
            # MIN_SESSION_SECONDS выкашивал давно закрытые сессии, время
            # которых уже лежит в агрегатах: сырые данные и агрегат
            # расходились молча.
            await self.conn.execute(
                """
                DELETE FROM voice_sessions
                WHERE is_open = 1
                  AND CAST(
                      (julianday(credited_until) - julianday(joined_at)) * 86400 AS INTEGER
                  ) < ?
                """,
                (min_session_seconds,),
            )
            cursor = await self.conn.execute(
                """
                UPDATE voice_sessions
                SET is_open = 0,
                    left_at = credited_until,
                    duration_seconds = CAST(
                        (julianday(credited_until) - julianday(joined_at)) * 86400 AS INTEGER
                    )
                WHERE is_open = 1
                """
            )
            closed = cursor.rowcount or 0
            await self.conn.commit()
        return closed

    async def prune_raw_sessions(self, before: datetime) -> int:
        async with self._lock:
            cursor = await self.conn.execute(
                "DELETE FROM voice_sessions WHERE is_open = 0 AND left_at < ?",
                (to_iso(before),),
            )
            await self.conn.commit()
        return cursor.rowcount or 0

    # --- Rich Presence (раздел 3.3) ---

    async def open_presence_session(
        self,
        *,
        guild_id: int,
        user_id: int,
        activity_type: str,
        activity_name: str,
        started_at: datetime,
    ) -> OpenPresence:
        stamp = to_iso(started_at)
        async with self._lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO presence_sessions
                    (guild_id, user_id, activity_type, activity_name,
                     started_at, is_open, credited_until)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (guild_id, user_id, activity_type, activity_name, stamp, stamp),
            )
            await self.conn.commit()
            session_id = cursor.lastrowid
        return OpenPresence(
            id=int(session_id),
            guild_id=guild_id,
            user_id=user_id,
            activity_type=activity_type,
            activity_name=activity_name,
            started_at=started_at,
            credited_until=started_at,
        )

    async def credit_presence(
        self, session: OpenPresence, until: datetime, *, min_session_seconds: int
    ) -> None:
        if (until - session.started_at).total_seconds() < min_session_seconds:
            return
        buckets: dict[str, int] = {}
        for day, _hour, seconds in split_into_hours(session.credited_until, until, self._tz):
            key = day.strftime(ISO_DATE)
            buckets[key] = buckets.get(key, 0) + seconds
        if not buckets:
            return

        duration = int(round((until - session.started_at).total_seconds()))
        async with self._lock:
            for day_key, seconds in buckets.items():
                await self.conn.execute(
                    """
                    INSERT INTO game_events_daily
                        (guild_id, user_id, activity_name, date, seconds)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, activity_name, date) DO UPDATE SET
                        seconds = seconds + excluded.seconds
                    """,
                    (session.guild_id, session.user_id, session.activity_name, day_key, seconds),
                )
            await self.conn.execute(
                "UPDATE presence_sessions SET credited_until = ?, duration_seconds = ?"
                " WHERE id = ?",
                (to_iso(until), duration, session.id),
            )
            await self.conn.commit()
        session.credited_until = until

    async def close_presence_session(
        self, session: OpenPresence, ended_at: datetime, *, min_session_seconds: int
    ) -> None:
        await self.credit_presence(session, ended_at, min_session_seconds=min_session_seconds)
        duration = int(round((ended_at - session.started_at).total_seconds()))
        async with self._lock:
            if duration < min_session_seconds:
                await self.conn.execute(
                    "DELETE FROM presence_sessions WHERE id = ?", (session.id,)
                )
            else:
                await self.conn.execute(
                    "UPDATE presence_sessions SET is_open = 0, ended_at = ?,"
                    " duration_seconds = ? WHERE id = ?",
                    (to_iso(ended_at), duration, session.id),
                )
            await self.conn.commit()

    async def close_orphaned_presence_sessions(self, *, min_session_seconds: int) -> int:
        async with self._lock:
            # Тот же порядок, что и для голосовых сессий: короткие удаляются
            # только среди осиротевших, чтобы не задеть уже начисленные.
            await self.conn.execute(
                """
                DELETE FROM presence_sessions
                WHERE is_open = 1
                  AND CAST(
                      (julianday(credited_until) - julianday(started_at)) * 86400 AS INTEGER
                  ) < ?
                """,
                (min_session_seconds,),
            )
            cursor = await self.conn.execute(
                """
                UPDATE presence_sessions
                SET is_open = 0,
                    ended_at = credited_until,
                    duration_seconds = CAST(
                        (julianday(credited_until) - julianday(started_at)) * 86400 AS INTEGER
                    )
                WHERE is_open = 1
                """
            )
            closed = cursor.rowcount or 0
            await self.conn.commit()
        return closed

    async def prune_raw_presence(self, before: datetime) -> int:
        async with self._lock:
            cursor = await self.conn.execute(
                "DELETE FROM presence_sessions WHERE is_open = 0 AND ended_at < ?",
                (to_iso(before),),
            )
            await self.conn.commit()
        return cursor.rowcount or 0

    async def top_games(
        self,
        guild_id: int,
        *,
        limit: int = 10,
        since: str | None = None,
        until: str | None = None,
    ) -> list[GameRow]:
        clause, extra = _period_clause(since, alias="g", until=until)
        async with self.conn.execute(
            f"""
            SELECT g.activity_name           AS activity_name,
                   SUM(g.seconds)            AS seconds,
                   COUNT(DISTINCT g.user_id) AS players
            FROM game_events_daily g
            LEFT JOIN users u ON u.user_id = g.user_id
            WHERE g.guild_id = ? AND COALESCE(u.is_bot, 0) = 0{clause}
            GROUP BY g.activity_name
            HAVING seconds > 0
            ORDER BY seconds DESC
            LIMIT ?
            """,
            (guild_id, *extra, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            GameRow(name=r["activity_name"], seconds=int(r["seconds"]), players=int(r["players"]))
            for r in rows
        ]

    async def user_top_game(
        self, guild_id: int, user_id: int, *, since: str | None = None
    ) -> GameRow | None:
        clause, extra = _period_clause(since, alias="g")
        async with self.conn.execute(
            f"""
            SELECT g.activity_name AS activity_name, SUM(g.seconds) AS seconds
            FROM game_events_daily g
            WHERE g.guild_id = ? AND g.user_id = ?{clause}
            GROUP BY g.activity_name
            HAVING seconds > 0
            ORDER BY seconds DESC
            LIMIT 1
            """,
            (guild_id, user_id, *extra),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return GameRow(name=row["activity_name"], seconds=int(row["seconds"]), players=1)

    # --- текстовая активность ---

    async def add_message_counts(
        self,
        rows: Sequence[tuple[int, int, int, str, int, int]],
        heatmap: Sequence[tuple[int, int, int, int, int]] = (),
    ) -> None:
        """rows: (guild_id, user_id, channel_id, date, message_count, char_count).

        heatmap: (guild_id, user_id, weekday, hour, message_count).
        """
        if not rows:
            return
        async with self._lock:
            await self.conn.executemany(
                """
                INSERT INTO message_events_daily
                    (guild_id, user_id, channel_id, date, message_count, char_count_approx)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, channel_id, date) DO UPDATE SET
                    message_count = message_count + excluded.message_count,
                    char_count_approx = char_count_approx + excluded.char_count_approx
                """,
                rows,
            )
            await self.conn.executemany(
                """
                INSERT INTO daily_activity_summary
                    (guild_id, user_id, date, voice_seconds, message_count, stream_seconds)
                VALUES (?, ?, ?, 0, ?, 0)
                ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                    message_count = message_count + excluded.message_count
                """,
                [(g, u, d, count) for g, u, _c, d, count, _ch in rows],
            )
            if heatmap:
                await self.conn.executemany(
                    """
                    INSERT INTO activity_heatmap (guild_id, weekday, hour, voice_seconds, message_count)
                    VALUES (?, ?, ?, 0, ?)
                    ON CONFLICT(guild_id, weekday, hour) DO UPDATE SET
                        message_count = message_count + excluded.message_count
                    """,
                    [(g, w, h, c) for g, _u, w, h, c in heatmap],
                )
                await self.conn.executemany(
                    """
                    INSERT INTO user_heatmap
                        (guild_id, user_id, weekday, hour, voice_seconds, message_count)
                    VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(guild_id, user_id, weekday, hour) DO UPDATE SET
                        message_count = message_count + excluded.message_count
                    """,
                    heatmap,
                )
            await self.conn.commit()

    # --- выборки ---

    async def leaderboard(
        self,
        guild_id: int,
        metric: str,
        *,
        limit: int,
        voice_weight: float,
        message_weight: float,
        since: str | None = None,
        until: str | None = None,
    ) -> list[LeaderboardRow]:
        order = METRIC_ORDER[metric]
        clause, extra = _period_clause(since, until=until)
        async with self.conn.execute(
            f"""
            SELECT s.user_id                     AS user_id,
                   u.username                    AS username,
                   SUM(s.voice_seconds)          AS voice_seconds,
                   SUM(s.message_count)          AS message_count,
                   SUM(s.stream_seconds)         AS stream_seconds,
                   SUM(s.voice_seconds) / 60.0 * ? + SUM(s.message_count) * ? AS combined_score
            FROM daily_activity_summary s
            LEFT JOIN users u ON u.user_id = s.user_id
            WHERE s.guild_id = ? AND COALESCE(u.is_bot, 0) = 0{clause}
            GROUP BY s.user_id
            HAVING {order} > 0
            ORDER BY {order} DESC, s.user_id ASC
            LIMIT ?
            """,
            (voice_weight, message_weight, guild_id, *extra, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_leaderboard_row(row) for row in rows]

    async def user_totals(
        self,
        guild_id: int,
        user_id: int,
        *,
        voice_weight: float,
        message_weight: float,
        since: str | None = None,
    ) -> LeaderboardRow | None:
        clause, extra = _period_clause(since)
        async with self.conn.execute(
            f"""
            SELECT s.user_id                     AS user_id,
                   u.username                    AS username,
                   SUM(s.voice_seconds)          AS voice_seconds,
                   SUM(s.message_count)          AS message_count,
                   SUM(s.stream_seconds)         AS stream_seconds,
                   SUM(s.voice_seconds) / 60.0 * ? + SUM(s.message_count) * ? AS combined_score
            FROM daily_activity_summary s
            LEFT JOIN users u ON u.user_id = s.user_id
            WHERE s.guild_id = ? AND s.user_id = ?{clause}
            GROUP BY s.user_id
            """,
            (voice_weight, message_weight, guild_id, user_id, *extra),
        ) as cursor:
            row = await cursor.fetchone()
        return _to_leaderboard_row(row) if row else None

    async def user_rank(
        self,
        guild_id: int,
        user_id: int,
        *,
        voice_weight: float,
        message_weight: float,
        since: str | None = None,
    ) -> tuple[int, int] | None:
        """Место пользователя по combined_score и общее число участников в рейтинге.

        Порядок тот же, что в `leaderboard`: очки по убыванию, при равенстве —
        меньший user_id выше. Иначе двое с одинаковым счётом видели бы у себя
        одно и то же место (и одинаковый бейдж на карточке), а в самом
        лидерборде стояли бы друг за другом.
        """
        clause, extra = _period_clause(since)
        async with self.conn.execute(
            f"""
            WITH scores AS (
                SELECT s.user_id AS user_id,
                       SUM(s.voice_seconds) / 60.0 * ? + SUM(s.message_count) * ? AS score
                FROM daily_activity_summary s
                LEFT JOIN users u ON u.user_id = s.user_id
                WHERE s.guild_id = ? AND COALESCE(u.is_bot, 0) = 0{clause}
                GROUP BY s.user_id
                HAVING score > 0
            )
            SELECT (SELECT COUNT(*) FROM scores) AS total,
                   (SELECT COUNT(*) + 1
                      FROM scores AS other
                      CROSS JOIN (SELECT score FROM scores WHERE user_id = ?) AS me
                     WHERE other.score > me.score
                        OR (other.score = me.score AND other.user_id < ?)) AS rank
            WHERE EXISTS (SELECT 1 FROM scores WHERE user_id = ?)
            """,
            (voice_weight, message_weight, guild_id, *extra, user_id, user_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["rank"]), int(row["total"])

    # --- данные для карточки профиля (раздел 3.5) ---

    async def user_daily_series(
        self,
        guild_id: int,
        user_id: int,
        *,
        since: str,
        voice_weight: float,
        message_weight: float,
    ) -> dict[str, DailyPoint]:
        """Активность по дням начиная с `since`. Пропущенные дни не возвращаются."""
        async with self.conn.execute(
            """
            SELECT date,
                   voice_seconds,
                   message_count,
                   voice_seconds / 60.0 * ? + message_count * ? AS score
            FROM daily_activity_summary
            WHERE guild_id = ? AND user_id = ? AND date >= ?
            ORDER BY date
            """,
            (voice_weight, message_weight, guild_id, user_id, since),
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            row["date"]: DailyPoint(
                date=row["date"],
                voice_seconds=int(row["voice_seconds"]),
                message_count=int(row["message_count"]),
                score=float(row["score"]),
            )
            for row in rows
        }

    async def user_favorite_voice_channel(
        self, guild_id: int, user_id: int, *, since: str | None = None
    ) -> tuple[int, int] | None:
        clause, extra = _period_clause(since, alias="v")
        async with self.conn.execute(
            f"""
            SELECT v.channel_id AS channel_id, SUM(v.voice_seconds) AS voice_seconds
            FROM voice_events_daily v
            WHERE v.guild_id = ? AND v.user_id = ?{clause}
            GROUP BY v.channel_id
            HAVING voice_seconds > 0
            ORDER BY voice_seconds DESC
            LIMIT 1
            """,
            (guild_id, user_id, *extra),
        ) as cursor:
            row = await cursor.fetchone()
        return (int(row["channel_id"]), int(row["voice_seconds"])) if row else None

    # --- сводка по серверу (раздел 3.4) ---

    async def guild_totals(
        self, guild_id: int, *, since: str | None = None, until: str | None = None
    ) -> GuildTotals:
        clause, extra = _period_clause(since, until=until)
        async with self.conn.execute(
            f"""
            SELECT COALESCE(SUM(s.voice_seconds), 0)  AS voice_seconds,
                   COALESCE(SUM(s.message_count), 0)  AS message_count,
                   COALESCE(SUM(s.stream_seconds), 0) AS stream_seconds,
                   COUNT(DISTINCT s.user_id)          AS active_users,
                   COUNT(DISTINCT s.date)             AS active_days
            FROM daily_activity_summary s
            LEFT JOIN users u ON u.user_id = s.user_id
            WHERE s.guild_id = ? AND COALESCE(u.is_bot, 0) = 0{clause}
            """,
            (guild_id, *extra),
        ) as cursor:
            row = await cursor.fetchone()
        return GuildTotals(
            voice_seconds=int(row["voice_seconds"]),
            message_count=int(row["message_count"]),
            stream_seconds=int(row["stream_seconds"]),
            active_users=int(row["active_users"]),
            active_days=int(row["active_days"]),
        )

    async def top_voice_channels(
        self, guild_id: int, *, limit: int = 3, since: str | None = None
    ) -> list[tuple[int, int]]:
        clause, extra = _period_clause(since, alias="v")
        async with self.conn.execute(
            f"""
            SELECT v.channel_id AS channel_id, SUM(v.voice_seconds) AS voice_seconds
            FROM voice_events_daily v
            LEFT JOIN users u ON u.user_id = v.user_id
            WHERE v.guild_id = ? AND COALESCE(u.is_bot, 0) = 0{clause}
            GROUP BY v.channel_id
            HAVING voice_seconds > 0
            ORDER BY voice_seconds DESC
            LIMIT ?
            """,
            (guild_id, *extra, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(int(r["channel_id"]), int(r["voice_seconds"])) for r in rows]

    async def top_text_channels(
        self, guild_id: int, *, limit: int = 3, since: str | None = None
    ) -> list[tuple[int, int]]:
        clause, extra = _period_clause(since, alias="m")
        async with self.conn.execute(
            f"""
            SELECT m.channel_id AS channel_id, SUM(m.message_count) AS message_count
            FROM message_events_daily m
            LEFT JOIN users u ON u.user_id = m.user_id
            WHERE m.guild_id = ? AND COALESCE(u.is_bot, 0) = 0{clause}
            GROUP BY m.channel_id
            HAVING message_count > 0
            ORDER BY message_count DESC
            LIMIT ?
            """,
            (guild_id, *extra, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(int(r["channel_id"]), int(r["message_count"])) for r in rows]

    async def busiest_weekday(
        self, guild_id: int, *, voice_weight: float, message_weight: float,
        since: str | None = None,
    ) -> int | None:
        """День недели в нотации strftime('%w') с максимальной активностью."""
        clause, extra = _period_clause(since)
        async with self.conn.execute(
            f"""
            SELECT CAST(strftime('%w', s.date) AS INTEGER) AS weekday
            FROM daily_activity_summary s
            LEFT JOIN users u ON u.user_id = s.user_id
            WHERE s.guild_id = ? AND COALESCE(u.is_bot, 0) = 0{clause}
            GROUP BY weekday
            HAVING SUM(s.voice_seconds) + SUM(s.message_count) > 0
            ORDER BY SUM(s.voice_seconds) / 60.0 * ? + SUM(s.message_count) * ? DESC
            LIMIT 1
            """,
            (guild_id, *extra, voice_weight, message_weight),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["weekday"]) if row else None

    async def busiest_hour(
        self,
        guild_id: int,
        *,
        voice_weight: float,
        message_weight: float,
        user_id: int | None = None,
    ) -> int | None:
        """Самый активный час суток за всё время (тепловая карта без дат)."""
        table = "user_heatmap" if user_id is not None else "activity_heatmap"
        clause = " AND user_id = ?" if user_id is not None else ""
        params = (guild_id, *( (user_id,) if user_id is not None else () ),
                  voice_weight, message_weight)
        async with self.conn.execute(
            f"""
            SELECT hour
            FROM {table}
            WHERE guild_id = ?{clause}
            GROUP BY hour
            HAVING SUM(voice_seconds) + SUM(message_count) > 0
            ORDER BY SUM(voice_seconds) / 60.0 * ? + SUM(message_count) * ? DESC
            LIMIT 1
            """,
            params,
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["hour"]) if row else None

    async def busiest_slot(
        self,
        guild_id: int,
        *,
        voice_weight: float,
        message_weight: float,
        user_id: int | None = None,
    ) -> tuple[int, int] | None:
        """Самая активная пара (день недели, час) — для карточки профиля."""
        table = "user_heatmap" if user_id is not None else "activity_heatmap"
        clause = " AND user_id = ?" if user_id is not None else ""
        params = (guild_id, *((user_id,) if user_id is not None else ()),
                  voice_weight, message_weight)
        async with self.conn.execute(
            f"""
            SELECT weekday, hour
            FROM {table}
            WHERE guild_id = ?{clause}
            GROUP BY weekday, hour
            HAVING SUM(voice_seconds) + SUM(message_count) > 0
            ORDER BY SUM(voice_seconds) / 60.0 * ? + SUM(message_count) * ? DESC
            LIMIT 1
            """,
            params,
        ) as cursor:
            row = await cursor.fetchone()
        return (int(row["weekday"]), int(row["hour"])) if row else None

    async def heatmap_grid(
        self,
        guild_id: int,
        *,
        voice_weight: float,
        message_weight: float,
        user_id: int | None = None,
    ) -> dict[tuple[int, int], float]:
        """Сетка (день недели, час) -> вес активности. Пустые слоты не возвращаются."""
        table = "user_heatmap" if user_id is not None else "activity_heatmap"
        clause = " AND user_id = ?" if user_id is not None else ""
        params = (voice_weight, message_weight, guild_id,
                  *((user_id,) if user_id is not None else ()))
        async with self.conn.execute(
            f"""
            SELECT weekday, hour,
                   SUM(voice_seconds) / 60.0 * ? + SUM(message_count) * ? AS score
            FROM {table}
            WHERE guild_id = ?{clause}
            GROUP BY weekday, hour
            HAVING score > 0
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return {(int(r["weekday"]), int(r["hour"])): float(r["score"]) for r in rows}


def _period_clause(
    since: str | None, alias: str = "s", until: str | None = None
) -> tuple[str, tuple]:
    """Фильтр периода для агрегатов. None с обеих сторон = за всё время.

    `until` включительно — периоды задаются календарными днями.
    """
    clause = ""
    params: list[str] = []
    if since is not None:
        clause += f" AND {alias}.date >= ?"
        params.append(since)
    if until is not None:
        clause += f" AND {alias}.date <= ?"
        params.append(until)
    return clause, tuple(params)


def _to_leaderboard_row(row: aiosqlite.Row) -> LeaderboardRow:
    return LeaderboardRow(
        user_id=row["user_id"],
        username=row["username"],
        voice_seconds=int(row["voice_seconds"] or 0),
        message_count=int(row["message_count"] or 0),
        stream_seconds=int(row["stream_seconds"] or 0),
        combined_score=float(row["combined_score"] or 0.0),
    )
