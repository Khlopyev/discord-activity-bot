"""Трекинг голосовой активности (раздел 3.1 брифа).

Модель: «сегмент» — отрезок времени с неизменной парой (канал, self_stream).
Любое изменение канала или флага трансляции закрывает текущий сегмент и
открывает новый. Переключение между каналами тем самым автоматически
считается сменой сессии, а не выходом.

Время начисляется инкрементально фоновой задачей (`VOICE_FLUSH_INTERVAL`),
поэтому аварийное падение бота теряет максимум один интервал, а не всю
сессию целиком.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from ..db import OpenSession
from ..filters import is_tracked_channel, is_tracked_user
from ..timeutil import utcnow

log = logging.getLogger(__name__)


class VoiceTracker(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.settings = bot.settings
        self._sessions: dict[tuple[int, int], OpenSession] = {}
        self._lock = asyncio.Lock()
        self._reconciled = False

        self.flush_loop.change_interval(seconds=self.config.voice_flush_interval)
        self.flush_loop.start()
        if self.config.raw_retention_days > 0:
            self.prune_loop.start()

    def cog_unload(self) -> None:
        self.flush_loop.cancel()
        self.prune_loop.cancel()

    # --- события ---

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # on_ready может прийти повторно после реконнекта — сверяемся каждый раз.
        await self.reconcile()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not self._reconciled:
            return
        if not is_tracked_user(member.guild.id, member, self.settings):
            # Отказавшийся от трекинга мог быть в войсе до отказа — закрываем сессию.
            await self.apply_state(member, None)
            return
        await self.apply_state(member, after)

    async def apply_state(self, member: discord.Member, state: discord.VoiceState | None) -> None:
        """Привести хранимую сессию участника в соответствие с его voice state."""
        desired: tuple[int, bool] | None = None
        if state is not None and is_tracked_channel(state.channel, self.config, self.settings):
            desired = (state.channel.id, bool(state.self_stream))

        key = (member.guild.id, member.id)
        async with self._lock:
            current = self._sessions.get(key)
            current_state = (current.channel_id, current.is_stream) if current else None
            if current_state == desired:
                return

            now = utcnow()
            if current is not None:
                await self.db.close_session(
                    current, now, min_session_seconds=self.config.min_session_seconds
                )
                self._sessions.pop(key, None)

            if desired is not None:
                channel_id, is_stream = desired
                self._sessions[key] = await self.db.open_voice_session(
                    guild_id=member.guild.id,
                    user_id=member.id,
                    channel_id=channel_id,
                    joined_at=now,
                    is_stream=is_stream,
                )
                await self.db.upsert_users([(member.id, str(member), False)])

    # --- синхронизация состояния после старта/реконнекта ---

    async def reconcile(self) -> None:
        """Закрыть «осиротевшие» сессии и открыть их для всех, кто сейчас в войсе.

        Время простоя бота не начисляется: осиротевшая сессия закрывается
        моментом последнего подтверждённого начисления.
        """
        # Исключения читаются синхронно на горячем пути, поэтому кэш настроек
        # должен быть заполнен до первого обращения.
        await self.settings.prime([guild.id for guild in self.bot.guilds])
        async with self._lock:
            self._sessions.clear()
            closed = await self.db.close_orphaned_sessions(
                min_session_seconds=self.config.min_session_seconds
            )
            if closed:
                log.info("Закрыто осиротевших голосовых сессий: %s", closed)

            now = utcnow()
            seen_users: list[tuple[int, str | None, bool]] = []
            for guild in self.bot.guilds:
                await self.db.upsert_guild(guild.id, guild.name)
                channels: list[discord.abc.GuildChannel] = [
                    *guild.voice_channels,
                    *guild.stage_channels,
                ]
                for channel in channels:
                    if not is_tracked_channel(channel, self.config, self.settings):
                        continue
                    for member in channel.members:
                        if not is_tracked_user(guild.id, member, self.settings):
                            continue
                        state = member.voice
                        self._sessions[(guild.id, member.id)] = await self.db.open_voice_session(
                            guild_id=guild.id,
                            user_id=member.id,
                            channel_id=channel.id,
                            joined_at=now,
                            is_stream=bool(state.self_stream) if state else False,
                        )
                        seen_users.append((member.id, str(member), False))

            await self.db.upsert_users(seen_users)
            self._reconciled = True
            log.info("Синхронизировано активных голосовых сессий: %s", len(self._sessions))

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
                    await self.db.credit_session(
                        session, now, min_session_seconds=self.config.min_session_seconds
                    )
                except Exception:
                    log.exception("Не удалось начислить время сессии %s", session.id)

    @tasks.loop(hours=24)
    async def prune_loop(self) -> None:
        cutoff = utcnow() - timedelta(days=self.config.raw_retention_days)
        deleted = await self.db.prune_raw_sessions(cutoff)
        if deleted:
            log.info("Удалено сырых голосовых сессий старше %s дн.: %s",
                     self.config.raw_retention_days, deleted)

    @prune_loop.before_loop
    async def before_prune(self) -> None:
        await self.bot.wait_until_ready()

    # --- завершение работы ---

    async def shutdown(self) -> None:
        """Корректно закрыть все открытые сессии перед остановкой бота."""
        self.flush_loop.cancel()
        self.prune_loop.cancel()
        async with self._lock:
            now = utcnow()
            for key, session in list(self._sessions.items()):
                try:
                    await self.db.close_session(
                        session, now, min_session_seconds=self.config.min_session_seconds
                    )
                except Exception:
                    log.exception("Не удалось закрыть сессию %s", session.id)
                self._sessions.pop(key, None)
