from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from bot.timeutil import (
    format_duration,
    format_hour_range,
    period_start,
    split_into_days,
    split_into_hours,
    sqlite_weekday,
    weekday_hour,
)

UTC = ZoneInfo("UTC")
MSK = ZoneInfo("Europe/Moscow")


def dt(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def test_interval_inside_one_day():
    assert split_into_days(dt(2026, 8, 17, 10), dt(2026, 8, 17, 11, 30), UTC) == [
        ("2026-08-17", 5400)
    ]


def test_empty_and_inverted_intervals():
    assert split_into_days(dt(2026, 8, 17, 10), dt(2026, 8, 17, 10), UTC) == []
    assert split_into_days(dt(2026, 8, 17, 11), dt(2026, 8, 17, 10), UTC) == []


def test_interval_crossing_midnight_is_split():
    result = split_into_days(dt(2026, 8, 17, 23, 30), dt(2026, 8, 18, 0, 45), UTC)
    assert result == [("2026-08-17", 1800), ("2026-08-18", 2700)]


def test_multi_day_interval_keeps_full_days():
    result = split_into_days(dt(2026, 8, 17, 22), dt(2026, 8, 20, 1), UTC)
    assert result == [
        ("2026-08-17", 7200),
        ("2026-08-18", 86400),
        ("2026-08-19", 86400),
        ("2026-08-20", 3600),
    ]


def test_day_boundary_follows_configured_timezone():
    # 20:30 UTC = 23:30 MSK -> сутки в MSK закончатся через 30 минут.
    result = split_into_days(dt(2026, 8, 17, 20, 30), dt(2026, 8, 17, 21, 30), MSK)
    assert result == [("2026-08-17", 1800), ("2026-08-18", 1800)]


def test_total_seconds_are_preserved():
    start, end = dt(2026, 3, 28, 12), dt(2026, 4, 2, 7, 13, 45)
    total = sum(seconds for _, seconds in split_into_days(start, end, MSK))
    assert total == int((end - start).total_seconds())


def test_split_into_hours_aligns_to_local_hour_boundaries():
    result = split_into_hours(dt(2026, 8, 17, 10, 40), dt(2026, 8, 17, 12, 10), UTC)
    assert result == [
        (date(2026, 8, 17), 10, 1200),
        (date(2026, 8, 17), 11, 3600),
        (date(2026, 8, 17), 12, 600),
    ]


def test_split_into_hours_respects_timezone_and_date_rollover():
    # 20:40 UTC = 23:40 MSK: час 23 закончится через 20 минут, дальше уже 18-е.
    result = split_into_hours(dt(2026, 8, 17, 20, 40), dt(2026, 8, 17, 21, 30), MSK)
    assert result == [
        (date(2026, 8, 17), 23, 1200),
        (date(2026, 8, 18), 0, 1800),
    ]


def test_split_into_hours_preserves_total_across_dst():
    # Переход на летнее время в Европе: последнее воскресенье марта.
    start, end = dt(2026, 3, 29, 0), dt(2026, 3, 29, 6)
    chunks = split_into_hours(start, end, MSK)
    assert sum(seconds for _, _, seconds in chunks) == int((end - start).total_seconds())


def test_sqlite_weekday_matches_strftime_notation():
    # 2026-08-17 — понедельник. strftime('%w'): 0=вс, значит понедельник = 1.
    assert sqlite_weekday(date(2026, 8, 17)) == 1
    assert sqlite_weekday(date(2026, 8, 16)) == 0  # воскресенье


def test_weekday_hour_uses_configured_timezone():
    assert weekday_hour(dt(2026, 8, 17, 20, 40), UTC) == (1, 20)
    assert weekday_hour(dt(2026, 8, 17, 22, 40), MSK) == (2, 1)  # уже вторник 01:40 МСК


def test_period_start():
    monday = date(2026, 8, 17)
    wednesday = date(2026, 8, 19)
    assert period_start(wednesday, "day") == "2026-08-19"
    assert period_start(wednesday, "week") == "2026-08-17"
    assert period_start(monday, "week") == "2026-08-17"
    assert period_start(wednesday, "month") == "2026-08-01"
    assert period_start(wednesday, "all") is None


def test_format_hour_range():
    assert format_hour_range(9) == "09:00–10:00"
    assert format_hour_range(23) == "23:00–00:00"


def test_format_duration():
    assert format_duration(45) == "45с"
    assert format_duration(720) == "12м"
    assert format_duration(3900) == "1ч 05м"
    assert format_duration(90000) == "1д 1ч 00м"
    assert format_duration(-5) == "0с"
