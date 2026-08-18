"""Работа со временем: единая тайм-зона агрегации и нарезка интервалов.

Раздел 7 брифа требует зафиксировать одну тайм-зону для «дневной» статистики.
Всё хранится в UTC, а границы суток и часов считаются в `Config.timezone`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ISO_DATE = "%Y-%m-%d"

# SQLite strftime('%w') отдаёт 0=воскресенье, Python date.weekday() — 0=понедельник.
WEEKDAY_NAMES_SQLITE = (
    "воскресенье",
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
)

PERIOD_TITLES = {
    "day": "за сегодня",
    "week": "за неделю",
    "month": "за месяц",
    "all": "за всё время",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """Сериализация datetime в UTC-строку для хранения в SQLite."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_date(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()


def day_key(dt: datetime, tz: ZoneInfo) -> str:
    return local_date(dt, tz).strftime(ISO_DATE)


def weekday_hour(dt: datetime, tz: ZoneInfo) -> tuple[int, int]:
    """(день недели в нотации SQLite: 0=вс, час 0-23) — ключ тепловой карты."""
    local = dt.astimezone(tz)
    return sqlite_weekday(local.date()), local.hour


def sqlite_weekday(day: date) -> int:
    """date.weekday() (0=пн) -> нотация strftime('%w') (0=вс)."""
    return (day.weekday() + 1) % 7


def period_start(today: date, period: str) -> str | None:
    """Нижняя граница периода включительно, 'YYYY-MM-DD'. None = всё время."""
    if period == "day":
        return today.strftime(ISO_DATE)
    if period == "week":
        return (today - timedelta(days=today.weekday())).strftime(ISO_DATE)
    if period == "month":
        return today.replace(day=1).strftime(ISO_DATE)
    return None


def split_into_hours(
    start: datetime, end: datetime, tz: ZoneInfo
) -> list[tuple[date, int, int]]:
    """Разрезать интервал [start, end) на куски по локальным часам.

    Возвращает список (локальная дата, час 0-23, секунды). На этом строятся
    сразу три агрегата: посуточный, поканальный и тепловая карта.

    Граница часа вычисляется прибавлением к UTC-моменту, а не арифметикой по
    локальным «стенным» часам — так переход на летнее время не ломает нарезку.
    """
    if end <= start:
        return []

    chunks: list[tuple[date, int, int]] = []
    cursor = start
    while cursor < end:
        local = cursor.astimezone(tz)
        into_hour = local.minute * 60 + local.second + local.microsecond / 1_000_000
        boundary = min(end, cursor + timedelta(seconds=3600 - into_hour))
        seconds = int(round((boundary - cursor).total_seconds()))
        if seconds > 0:
            chunks.append((local.date(), local.hour, seconds))
        cursor = boundary

    return chunks


def split_into_days(start: datetime, end: datetime, tz: ZoneInfo) -> list[tuple[str, int]]:
    """То же, но свёрнутое до суток: (день 'YYYY-MM-DD', секунды).

    Нужно, чтобы голосовая сессия, пересекающая полночь, корректно легла
    в две дневные записи.
    """
    buckets: dict[str, int] = {}
    for day, _hour, seconds in split_into_hours(start, end, tz):
        key = day.strftime(ISO_DATE)
        buckets[key] = buckets.get(key, 0) + seconds
    return sorted(buckets.items())


def format_duration(seconds: int) -> str:
    """Человекочитаемая длительность: '3д 4ч 05м', '4ч 05м', '12м', '45с'."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}д {hours}ч {minutes:02d}м"
    if hours:
        return f"{hours}ч {minutes:02d}м"
    if minutes:
        return f"{minutes}м"
    return f"{secs}с"


def format_hour_range(hour: int) -> str:
    return f"{hour:02d}:00–{(hour + 1) % 24:02d}:00"
