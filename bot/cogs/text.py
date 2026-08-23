"""Трекинг текстовой активности (раздел 3.2 брифа).

Содержимое сообщений не хранится — только счётчики (раздел 7, приватность).
Запись в БД идёт батчами по таймеру, а не на каждое сообщение (раздел 7,
производительность).
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from ..filters import is_tracked_channel, is_tracked_user
from ..timeutil import day_key, weekday_hour

log = logging.getLogger(__name__)

# Ключ буфера: (guild_id, user_id, channel_id, date)
BufferKey = tuple[int, int, int, str]
# Ключ тепловой карты: (guild_id, user_id, weekday, hour)
HeatKey = tuple[int, int, int, int]


class TextTracker(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.settings = bot.settings
        self._buffer: dict[BufferKey, list[int]] = {}
        self._heat: dict[HeatKey, int] = {}
        self._users: dict[int, str] = {}
        self._lock = asyncio.Lock()

        self.flush_loop.change_interval(seconds=self.config.message_flush_interval)
        self.flush_loop.start()

    def cog_unload(self) -> None:
        self.flush_loop.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if not is_tracked_user(message.guild.id, message.author, self.settings):
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        # Исключения читаются синхронно и до первой загрузки выглядят пустыми.
        # Сообщения, в отличие от голоса, не ждут синхронизации после старта,
        # поэтому в первые мгновения работы бота исключённый канал успевал
        # посчитаться. После первого обращения это просто попадание в словарь.
        await self.settings.get(message.guild.id)
        if not is_tracked_channel(message.channel, self.config, self.settings):
            return
        # Команды самого бота не считаем активностью.
        if message.content.startswith(self.config.command_prefix):
            return

        key: BufferKey = (
            message.guild.id,
            message.author.id,
            message.channel.id,
            day_key(message.created_at, self.config.timezone),
        )
        weekday, hour = weekday_hour(message.created_at, self.config.timezone)
        heat_key: HeatKey = (message.guild.id, message.author.id, weekday, hour)
        chars = len(message.content) if self.config.track_char_count else 0

        async with self._lock:
            entry = self._buffer.get(key)
            if entry is None:
                self._buffer[key] = [1, chars]
            else:
                entry[0] += 1
                entry[1] += chars
            self._heat[heat_key] = self._heat.get(heat_key, 0) + 1
            self._users[message.author.id] = str(message.author)

    @tasks.loop(seconds=30)
    async def flush_loop(self) -> None:
        await self.flush()

    @flush_loop.before_loop
    async def before_flush(self) -> None:
        await self.bot.wait_until_ready()

    async def flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            rows = [
                (guild_id, user_id, channel_id, date, count, chars)
                for (guild_id, user_id, channel_id, date), (count, chars) in self._buffer.items()
            ]
            heat = [(gid, uid, weekday, hour, count)
                    for (gid, uid, weekday, hour), count in self._heat.items()]
            users = [(uid, name, False) for uid, name in self._users.items()]
            self._buffer.clear()
            self._heat.clear()
            self._users.clear()

        try:
            await self.db.add_message_counts(rows, heat)
            await self.db.upsert_users(users)
        except Exception:
            log.exception("Не удалось записать счётчики сообщений, возвращаю в буфер")
            async with self._lock:
                for guild_id, user_id, channel_id, date, count, chars in rows:
                    key = (guild_id, user_id, channel_id, date)
                    entry = self._buffer.get(key)
                    if entry is None:
                        self._buffer[key] = [count, chars]
                    else:
                        entry[0] += count
                        entry[1] += chars
                for gid, uid, weekday, hour, count in heat:
                    heat_key = (gid, uid, weekday, hour)
                    self._heat[heat_key] = self._heat.get(heat_key, 0) + count

    async def shutdown(self) -> None:
        self.flush_loop.cancel()
        await self.flush()
