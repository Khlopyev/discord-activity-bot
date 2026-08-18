"""Настройки гильдий и отказы от трекинга с кэшем в памяти.

Оба набора данных читаются на горячем пути (каждое сообщение, каждое событие
голоса), поэтому держатся в памяти и перечитываются только при изменении.
Процесс один, так что кэш не может разъехаться с БД.
"""

from __future__ import annotations

import logging

from .db import Database

log = logging.getLogger(__name__)

EXCLUDED_CHANNELS = "excluded_channels"
SUMMARY_CHANNEL = "summary_channel"
SUMMARY_WEEKLY = "summary_weekly"
SUMMARY_MONTHLY = "summary_monthly"
LAST_WEEKLY = "last_weekly_summary"
LAST_MONTHLY = "last_monthly_summary"


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._guilds: dict[int, dict] = {}
        self._optouts: set[tuple[int, int]] = set()

    async def load(self) -> None:
        self._optouts = await self.db.fetch_optouts()
        self._guilds.clear()
        log.info("Загружено отказов от трекинга: %s", len(self._optouts))

    # --- настройки гильдии ---

    async def get(self, guild_id: int) -> dict:
        cached = self._guilds.get(guild_id)
        if cached is None:
            cached = await self.db.get_guild_settings(guild_id)
            self._guilds[guild_id] = cached
        return cached

    async def update(self, guild_id: int, patch: dict) -> dict:
        settings = await self.db.update_guild_settings(guild_id, patch)
        self._guilds[guild_id] = settings
        return settings

    def excluded_channels(self, guild_id: int) -> frozenset[int]:
        """Синхронный доступ для горячего пути — до первой загрузки пусто."""
        settings = self._guilds.get(guild_id)
        if not settings:
            return frozenset()
        return frozenset(settings.get(EXCLUDED_CHANNELS, ()))

    async def exclude_channel(self, guild_id: int, channel_id: int) -> bool:
        """True, если канал был добавлен; False — если уже был исключён."""
        settings = await self.get(guild_id)
        current = list(settings.get(EXCLUDED_CHANNELS, []))
        if channel_id in current:
            return False
        current.append(channel_id)
        await self.update(guild_id, {EXCLUDED_CHANNELS: current})
        return True

    async def include_channel(self, guild_id: int, channel_id: int) -> bool:
        settings = await self.get(guild_id)
        current = list(settings.get(EXCLUDED_CHANNELS, []))
        if channel_id not in current:
            return False
        current.remove(channel_id)
        await self.update(guild_id, {EXCLUDED_CHANNELS: current})
        return True

    # --- отказ от трекинга ---

    def is_opted_out(self, guild_id: int, user_id: int) -> bool:
        return (guild_id, user_id) in self._optouts

    async def set_optout(self, guild_id: int, user_id: int, opted_out: bool) -> None:
        await self.db.set_optout(guild_id, user_id, opted_out)
        if opted_out:
            self._optouts.add((guild_id, user_id))
        else:
            self._optouts.discard((guild_id, user_id))

    async def prime(self, guild_ids: list[int]) -> None:
        """Прогреть кэш настроек, чтобы горячий путь не ждал БД."""
        for guild_id in guild_ids:
            await self.get(guild_id)
