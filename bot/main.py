"""Точка входа бота."""

from __future__ import annotations

import logging
import sys

import discord
from discord.ext import commands

from .config import Config
from .db import Database
from .settings import SettingsStore
from .stats import StatsService
from .cogs.admin import Admin
from .cogs.leaderboard import Leaderboard
from .cogs.presence import PresenceTracker
from .cogs.slash import SlashCommands
from .cogs.summaries import Summaries
from .cogs.text import TextTracker
from .cogs.voice import VoiceTracker

log = logging.getLogger("bot")

TRACKER_COGS = ("VoiceTracker", "TextTracker", "PresenceTracker")


class ActivityBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        # GUILD_VOICE_STATES входит в Intents.default(); MESSAGE_CONTENT —
        # привилегированный intent, нужен и для счётчика символов, и для
        # текстовых команд (раздел 7 брифа).
        intents.message_content = True
        if config.enable_presence_tracking:
            # Оба привилегированные. GUILD_PRESENCES даёт сами активности,
            # SERVER MEMBERS — кэш участников, без которого при старте бот
            # видит только тех, кто попадался в войсе или сообщениях.
            intents.presences = True
            intents.members = True

        super().__init__(
            command_prefix=commands.when_mentioned_or(config.command_prefix),
            intents=intents,
            help_command=commands.DefaultHelpCommand(no_category="Команды"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.db = Database(config.database_path, config.timezone)
        self.settings = SettingsStore(self.db)
        self.stats = StatsService(self.db, config)
        self._synced_guilds: set[int] = set()

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.settings.load()
        await self.add_cog(VoiceTracker(self))
        await self.add_cog(TextTracker(self))
        if self.config.enable_presence_tracking:
            await self.add_cog(PresenceTracker(self))
        await self.add_cog(Leaderboard(self))
        await self.add_cog(Summaries(self))
        if self.config.enable_slash_commands:
            await self.add_cog(SlashCommands(self))
            await self.add_cog(Admin(self))

    async def on_ready(self) -> None:
        log.info("Вошли как %s (id=%s), серверов: %s",
                 self.user, getattr(self.user, "id", "?"), len(self.guilds))
        await self.settings.prime([guild.id for guild in self.guilds])
        if self.config.enable_slash_commands:
            await self.sync_commands()

    async def sync_commands(self) -> None:
        """Синхронизация слэш-команд по гильдиям — они появляются сразу.

        Глобальная синхронизация распространяется до часа, поэтому для бота
        на небольшом числе серверов гильдийная удобнее. Делается один раз за
        запуск, чтобы реконнекты не упирались в лимиты Discord.
        """
        for guild in self.guilds:
            if guild.id in self._synced_guilds:
                continue
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                log.warning(
                    "Нет доступа к слэш-командам на сервере %s. Бот приглашён без scope "
                    "applications.commands — переприглашение по ссылке с этим scope починит.",
                    guild.name,
                )
                continue
            except discord.HTTPException:
                log.exception("Не удалось синхронизировать слэш-команды на %s", guild.name)
                continue
            self._synced_guilds.add(guild.id)
            log.info("Слэш-команд синхронизировано на %s: %s", guild.name, len(synced))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.upsert_guild(guild.id, guild.name)
        if self.config.enable_slash_commands:
            await self.sync_commands()

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.reply("Команда работает только на сервере.", mention_author=False)
            return
        if isinstance(error, (commands.BadArgument, commands.UserInputError)):
            await ctx.reply(f"Не понял аргументы: {error}", mention_author=False)
            return
        log.exception("Ошибка в команде %s", ctx.command, exc_info=error)
        await ctx.reply("Что-то пошло не так, подробности в логах.", mention_author=False)

    async def close(self) -> None:
        # Досбрасываем буферы и закрываем открытые голосовые сессии до разрыва
        # соединения, иначе последний интервал активности потеряется.
        for name in TRACKER_COGS:
            cog = self.get_cog(name)
            if cog is not None:
                try:
                    await cog.shutdown()
                except Exception:
                    log.exception("Ошибка при завершении кога %s", name)
        await super().close()
        await self.db.close()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)

    try:
        config = Config.from_env()
    except RuntimeError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    bot = ActivityBot(config)
    try:
        bot.run(config.token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        needed = ["Message Content Intent"]
        if config.enable_presence_tracking:
            needed += ["Presence Intent", "Server Members Intent"]
        print(
            "Discord отклонил привилегированные intents.\n"
            "Включите в Developer Portal (Applications → ваш бот → Bot → "
            "Privileged Gateway Intents):\n  - " + "\n  - ".join(needed) + "\n"
            "Либо выключите трекинг игр: ENABLE_PRESENCE_TRACKING=false в .env.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except discord.LoginFailure:
        print("Неверный DISCORD_TOKEN.", file=sys.stderr)
        raise SystemExit(1)
