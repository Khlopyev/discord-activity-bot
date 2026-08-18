"""Трекинг Rich Presence — «во что играют» (раздел 3.3 брифа, этап v3).

Модель повторяет голосовой трекинг: сегмент — отрезок с неизменным названием
активности, время начисляется инкрементально фоновой задачей.

Ограничения платформы (раздел 8), которые здесь ничем не лечатся:

* нужен привилегированный intent `GUILD_PRESENCES`, включается в Developer Portal;
* пользователи, отключившие «Отображать текущую активность» в настройках
  приватности Discord, боту не видны вообще — это не баг, а решение самого
  пользователя, и обойти его нельзя.

Это независимый от `self_stream` источник данных: «Go Live» в голосовом канале
приходит через voice state и считается в `VoiceTracker`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from ..db import OpenPresence
from ..filters import is_tracked_user
from ..timeutil import utcnow

log = logging.getLogger(__name__)


def tracked_activity(
    member: discord.Member, allowed_types: frozenset[str]
) -> tuple[str, str] | None:
    """Первая релевантная активность участника или None.

    У человека одновременно может быть и игра, и Spotify — берём первую
    подходящую по настроенному списку типов.
    """
    for activity in member.activities:
        type_name = getattr(activity.type, "name", None)
        if type_name in allowed_types and activity.name:
            return type_name, activity.name
    return None


class PresenceTracker(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.settings = bot.settings
        self._sessions: dict[tuple[int, int], OpenPresence] = {}
        self._lock = asyncio.Lock()
        self._reconciled = False

        self.flush_loop.change_interval(seconds=self.config.presence_flush_interval)
        self.flush_loop.start()
        if self.config.raw_retention_days > 0:
            self.prune_loop.start()

    def cog_unload(self) -> None:
        self.flush_loop.cancel()
        self.prune_loop.cancel()

    # --- события ---

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.reconcile()

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._reconciled:
            return
        await self.apply_activity(after)

    async def apply_activity(self, member: discord.Member) -> None:
        """Привести хранимую сессию к текущей активности участника."""
        if is_tracked_user(member.guild.id, member, self.settings):
            desired = tracked_activity(member, self.config.tracked_activity_types)
        else:
            # Отказ от трекинга закрывает уже открытую игровую сессию.
            desired = None
        key = (member.guild.id, member.id)

        async with self._lock:
            current = self._sessions.get(key)
            current_state = (
                (current.activity_type, current.activity_name) if current else None
            )
            if current_state == desired:
                return

            now = utcnow()
            if current is not None:
                await self.db.close_presence_session(
                    current, now, min_session_seconds=self.config.min_session_seconds
                )
                self._sessions.pop(key, None)

            if desired is not None:
                activity_type, activity_name = desired
                self._sessions[key] = await self.db.open_presence_session(
                    guild_id=member.guild.id,
                    user_id=member.id,
                    activity_type=activity_type,
                    activity_name=activity_name,
                    started_at=now,
                )
                await self.db.upsert_users([(member.id, str(member), False)])

    # --- синхронизация после старта ---

    async def reconcile(self) -> None:
        await self.settings.prime([guild.id for guild in self.bot.guilds])
        async with self._lock:
            self._sessions.clear()
            closed = await self.db.close_orphaned_presence_sessions(
                min_session_seconds=self.config.min_session_seconds
            )
            if closed:
                log.info("Закрыто осиротевших presence-сессий: %s", closed)

            now = utcnow()
            seen_users: list[tuple[int, str | None, bool]] = []
            for guild in self.bot.guilds:
                for member in guild.members:
                    if not is_tracked_user(guild.id, member, self.settings):
                        continue
                    desired = tracked_activity(member, self.config.tracked_activity_types)
                    if desired is None:
                        continue
                    activity_type, activity_name = desired
                    self._sessions[(guild.id, member.id)] = await self.db.open_presence_session(
                        guild_id=guild.id,
                        user_id=member.id,
                        activity_type=activity_type,
                        activity_name=activity_name,
                        started_at=now,
                    )
                    seen_users.append((member.id, str(member), False))

            await self.db.upsert_users(seen_users)
            self._reconciled = True
            log.info("Синхронизировано активных presence-сессий: %s", len(self._sessions))

    # --- фоновые задачи ---

    @tasks.loop(seconds=60)
    async def flush_loop(self) -> None:
        await self.flush()

    @flush_loop.before_loop
    async def before_flush(self) -> None:
        await self.bot.wait_until_ready()

    async def flush(self) -> None:
        async with self._lock:
            now = utcnow()
            for session in list(self._sessions.values()):
                try:
                    await self.db.credit_presence(
                        session, now, min_session_seconds=self.config.min_session_seconds
                    )
                except Exception:
                    log.exception("Не удалось начислить время presence-сессии %s", session.id)

    @tasks.loop(hours=24)
    async def prune_loop(self) -> None:
        cutoff = utcnow() - timedelta(days=self.config.raw_retention_days)
        deleted = await self.db.prune_raw_presence(cutoff)
        if deleted:
            log.info("Удалено сырых presence-сессий: %s", deleted)

    @prune_loop.before_loop
    async def before_prune(self) -> None:
        await self.bot.wait_until_ready()

    async def shutdown(self) -> None:
        self.flush_loop.cancel()
        self.prune_loop.cancel()
        async with self._lock:
            now = utcnow()
            for key, session in list(self._sessions.items()):
                try:
                    await self.db.close_presence_session(
                        session, now, min_session_seconds=self.config.min_session_seconds
                    )
                except Exception:
                    log.exception("Не удалось закрыть presence-сессию %s", session.id)
                self._sessions.pop(key, None)
