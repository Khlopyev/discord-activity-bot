"""Замер тяжёлых запросов статистики на синтетической базе.

Нужен, чтобы решения про индексы принимались по цифрам, а не на глаз.
Заливает выдуманный сервер заданного размера и меряет запросы, из которых
собираются /leaderboard, /serverstats, /games и карточка профиля.

    python -m tools.bench_queries --users 200 --days 365 --channels 5

Данные пишутся напрямую в SQLite: через API бота заливка заняла бы часы.
Схему и индексы при этом создаёт сам Database, так что меряется ровно то,
что поедет на сервер.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sqlite3
import tempfile
import time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from bot.db import Database

UTC = ZoneInfo("UTC")
GUILD = 1
WEIGHTS = {"voice_weight": 1.0, "message_weight": 2.0}
TODAY = date(2026, 8, 23)
# Каждый пятидесятый — бот: запросы отсеивают их join'ом, и это часть работы.
BOT_EVERY = 50


def seed(path: str, users: int, days: int, channels: int, games: int = 5) -> dict[str, int]:
    start = TODAY - timedelta(days=days - 1)
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range(days)]
    con = sqlite3.connect(path)
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA journal_mode=OFF")

    con.executemany(
        "INSERT OR REPLACE INTO users VALUES (?,?,?,?)",
        [(u, f"user{u}", int(u % BOT_EVERY == 0), f"{TODAY}T00:00:00+00:00")
         for u in range(1, users + 1)],
    )
    con.executemany(
        "INSERT OR REPLACE INTO daily_activity_summary VALUES (?,?,?,?,?,?)",
        ((GUILD, u, d, random.randint(0, 14400), random.randint(0, 200),
          random.randint(0, 1800))
         for u in range(1, users + 1) for d in dates),
    )
    con.executemany(
        "INSERT OR REPLACE INTO message_events_daily VALUES (?,?,?,?,?,?)",
        ((GUILD, u, 1000 + c, d, random.randint(0, 60), random.randint(0, 3000))
         for u in range(1, users + 1) for c in range(channels) for d in dates),
    )
    con.executemany(
        "INSERT OR REPLACE INTO voice_events_daily VALUES (?,?,?,?,?,?)",
        ((GUILD, u, 2000 + c, d, random.randint(0, 3600), random.randint(0, 600))
         for u in range(1, users + 1) for c in range(channels) for d in dates),
    )
    con.executemany(
        "INSERT OR REPLACE INTO game_events_daily VALUES (?,?,?,?,?)",
        ((GUILD, u, f"Игра {g}", d, random.randint(0, 7200))
         for u in range(1, users + 1) for g in range(games) for d in dates),
    )
    con.commit()
    sizes = {
        table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("daily_activity_summary", "message_events_daily",
                      "voice_events_daily", "game_events_daily")
    }
    con.close()
    return sizes


async def measure(db: Database, runs: int) -> list[tuple[str, float]]:
    week = (TODAY - timedelta(days=6)).isoformat()
    cases = (
        ("leaderboard, всё время", lambda: db.leaderboard(GUILD, "combined", limit=25, **WEIGHTS)),
        ("leaderboard, неделя",
         lambda: db.leaderboard(GUILD, "combined", limit=25, since=week, **WEIGHTS)),
        ("guild_totals", lambda: db.guild_totals(GUILD)),
        ("top_voice_channels", lambda: db.top_voice_channels(GUILD)),
        ("top_text_channels", lambda: db.top_text_channels(GUILD)),
        ("top_games", lambda: db.top_games(GUILD, limit=10)),
        ("busiest_weekday", lambda: db.busiest_weekday(GUILD, **WEIGHTS)),
        ("user_rank", lambda: db.user_rank(GUILD, 1, **WEIGHTS)),
        ("user_favorite_voice_channel", lambda: db.user_favorite_voice_channel(GUILD, 1)),
        ("user_top_game", lambda: db.user_top_game(GUILD, 1)),
    )
    results = []
    for label, factory in cases:
        best = None
        for _ in range(runs):
            started = time.perf_counter()
            await factory()
            elapsed = (time.perf_counter() - started) * 1000
            best = elapsed if best is None else min(best, elapsed)
        results.append((label, best))
    return results


async def run(users: int, days: int, channels: int, runs: int) -> None:
    directory = tempfile.mkdtemp(prefix="acb-bench-")
    path = os.path.join(directory, "bench.db")

    db = Database(path, UTC)
    await db.connect()
    await db.upsert_guild(GUILD, "bench")
    await db.close()

    started = time.perf_counter()
    sizes = seed(path, users, days, channels)
    print(f"Залито за {time.perf_counter() - started:.0f} с, "
          f"файл {os.path.getsize(path) / 1024 / 1024:.0f} МБ")
    for table, count in sizes.items():
        print(f"  {table:<26}{count:>12,} строк".replace(",", " "))

    db = Database(path, UTC)
    started = time.perf_counter()
    await db.connect()
    print(f"Открытие базы (со сбором статистики): {time.perf_counter() - started:.1f} с")
    print()

    worst = 0.0
    for label, best in await measure(db, runs):
        marker = "  <-- дольше секунды" if best > 1000 else ""
        print(f"  {label:<30}{best:>9.1f} мс{marker}")
        worst = max(worst, best)
    await db.close()
    print()
    print(f"Самый медленный запрос: {worst:.0f} мс. Лимит Discord на ответ — 3000 мс.")
    print(f"База осталась в {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--channels", type=int, default=5)
    parser.add_argument("--runs", type=int, default=5, help="повторов на запрос, берётся лучший")
    parser.add_argument("--seed", type=int, default=20260823)
    args = parser.parse_args()

    random.seed(args.seed)
    asyncio.run(run(args.users, args.days, args.channels, args.runs))


if __name__ == "__main__":
    main()
