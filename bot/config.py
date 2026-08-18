"""Конфигурация бота, читается из переменных окружения / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом, получено: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} должен быть >= {minimum}, получено: {value}")
    return value


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть числом, получено: {raw!r}") from exc


def _id_set(name: str) -> frozenset[int]:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return frozenset()
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    try:
        return frozenset(int(p) for p in parts if p)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть списком ID через запятую, получено: {raw!r}") from exc


KNOWN_ACTIVITY_TYPES = frozenset({"playing", "streaming", "listening", "watching"})


def _activity_types(name: str) -> frozenset[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        # Раздел 3.3 брифа: релевантен прежде всего Playing.
        return frozenset({"playing"})
    values = {p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()}
    unknown = values - KNOWN_ACTIVITY_TYPES
    if unknown:
        raise RuntimeError(
            f"{name}: неизвестные типы активности {sorted(unknown)}. "
            f"Доступны: {sorted(KNOWN_ACTIVITY_TYPES)}"
        )
    return frozenset(values)


@dataclass(frozen=True)
class Config:
    token: str
    command_prefix: str
    database_path: str
    timezone: ZoneInfo
    timezone_name: str
    excluded_channel_ids: frozenset[int]
    exclude_afk_channel: bool
    min_session_seconds: int
    voice_flush_interval: int
    message_flush_interval: int
    leaderboard_cache_ttl: int
    track_char_count: bool
    enable_slash_commands: bool
    enable_presence_tracking: bool
    tracked_activity_types: frozenset[str]
    presence_flush_interval: int
    combined_voice_weight: float
    combined_message_weight: float
    raw_retention_days: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        token = (os.getenv("DISCORD_TOKEN") or "").strip()
        if not token:
            raise RuntimeError(
                "DISCORD_TOKEN не задан. Скопируйте .env.example в .env и впишите токен бота."
            )

        tz_name = (os.getenv("AGG_TIMEZONE") or "UTC").strip()
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise RuntimeError(f"AGG_TIMEZONE={tz_name!r} — неизвестная тайм-зона") from exc

        return cls(
            token=token,
            command_prefix=(os.getenv("COMMAND_PREFIX") or "!").strip() or "!",
            database_path=(os.getenv("DATABASE_PATH") or "data/activity.db").strip(),
            timezone=tz,
            timezone_name=tz_name,
            excluded_channel_ids=_id_set("EXCLUDED_CHANNEL_IDS"),
            exclude_afk_channel=_bool("EXCLUDE_AFK_CHANNEL", True),
            min_session_seconds=_int("MIN_SESSION_SECONDS", 10, minimum=0),
            voice_flush_interval=_int("VOICE_FLUSH_INTERVAL", 60, minimum=5),
            message_flush_interval=_int("MESSAGE_FLUSH_INTERVAL", 30, minimum=5),
            leaderboard_cache_ttl=_int("LEADERBOARD_CACHE_TTL", 60, minimum=0),
            track_char_count=_bool("TRACK_CHAR_COUNT", True),
            enable_slash_commands=_bool("ENABLE_SLASH_COMMANDS", True),
            enable_presence_tracking=_bool("ENABLE_PRESENCE_TRACKING", False),
            tracked_activity_types=_activity_types("TRACKED_ACTIVITY_TYPES"),
            presence_flush_interval=_int("PRESENCE_FLUSH_INTERVAL", 60, minimum=5),
            combined_voice_weight=_float("COMBINED_VOICE_WEIGHT", 1.0),
            combined_message_weight=_float("COMBINED_MESSAGE_WEIGHT", 2.0),
            raw_retention_days=_int("RAW_RETENTION_DAYS", 90, minimum=0),
        )
