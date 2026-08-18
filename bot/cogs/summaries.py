"""Автоматические сводки в канал по расписанию (раздел 3.4 брифа, этап v5).

Отдельной cron-задачи не заводим: бот и так работает постоянно, поэтому раз в
несколько минут проверяется, не закрылся ли очередной период. Отметка о
последней отправленной сводке лежит в настройках гильдии — благодаря ей
перезапуск бота не приводит ни к повтору, ни к пропуску.

Сводка всегда за **завершённый** период: в понедельник приходит итог прошлой
недели, первого числа — итог прошлого месяца.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import discord
from discord.ext import commands, tasks

from ..settings import (
    LAST_MONTHLY,
    LAST_WEEKLY,
    SUMMARY_CHANNEL,
    SUMMARY_MONTHLY,
    SUMMARY_WEEKLY,
)
from ..timeutil import ISO_DATE, format_duration, local_date, utcnow

log = logging.getLogger(__name__)

CHECK_INTERVAL_MINUTES = 10
TOP_IN_SUMMARY = 5


def previous_week(today: date) -> tuple[date, date]:
    """Границы прошлой календарной недели (пн–вс) включительно."""
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def previous_month(today: date) -> tuple[date, date]:
    """Границы прошлого календарного месяца включительно."""
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


class Summaries(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.settings = bot.settings
        self.stats = bot.stats
        self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def tick(self) -> None:
        today = local_date(utcnow(), self.config.timezone)
        for guild in self.bot.guilds:
            try:
                await self._check_guild(guild, today)
            except Exception:
                log.exception("Не удалось отправить сводку для %s", guild.name)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_guild(self, guild: discord.Guild, today: date) -> None:
        settings = await self.settings.get(guild.id)
        channel_id = settings.get(SUMMARY_CHANNEL)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            log.warning("Канал сводок %s на %s недоступен", channel_id, guild.name)
            return

        if settings.get(SUMMARY_WEEKLY, True):
            start, end = previous_week(today)
            marker = start.strftime(ISO_DATE)
            if settings.get(LAST_WEEKLY) != marker:
                await self._post(guild, channel, "недели", start, end)
                await self.settings.update(guild.id, {LAST_WEEKLY: marker})

        if settings.get(SUMMARY_MONTHLY, True):
            start, end = previous_month(today)
            marker = start.strftime("%Y-%m")
            if settings.get(LAST_MONTHLY) != marker:
                await self._post(guild, channel, "месяца", start, end)
                await self.settings.update(guild.id, {LAST_MONTHLY: marker})

    async def _post(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        period_name: str,
        start: date,
        end: date,
    ) -> None:
        embed = await self.build_summary(guild, period_name, start, end)
        if embed is None:
            return
        await channel.send(embed=embed)
        log.info("Отправлена сводка %s для %s", period_name, guild.name)

    async def build_summary(
        self, guild: discord.Guild, period_name: str, start: date, end: date
    ) -> discord.Embed | None:
        since, until = start.strftime(ISO_DATE), end.strftime(ISO_DATE)
        weights = {
            "voice_weight": self.config.combined_voice_weight,
            "message_weight": self.config.combined_message_weight,
        }

        totals = await self.db.guild_totals(guild.id, since=since, until=until)
        if totals.active_users == 0:
            # Пустой период не спамим — сводка появится, когда будет о чём.
            return None

        embed = discord.Embed(
            title=f"Итоги {period_name} — {guild.name}",
            description=f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}",
            colour=discord.Colour.blurple(),
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="В голосовых", value=format_duration(totals.voice_seconds))
        embed.add_field(name="Сообщений", value=str(totals.message_count))
        embed.add_field(name="Активных участников", value=str(totals.active_users))

        leaders = await self.db.leaderboard(
            guild.id, "combined", limit=TOP_IN_SUMMARY, since=since, until=until, **weights
        )
        if leaders:
            embed.add_field(
                name="Самые активные",
                value="\n".join(
                    f"**{index}.** {discord.utils.escape_markdown(self.stats.display_name(guild, row))}"
                    f" — {row.combined_score:.0f} очк."
                    for index, row in enumerate(leaders, start=1)
                ),
                inline=False,
            )

        if self.config.enable_presence_tracking:
            games = await self.db.top_games(guild.id, limit=3, since=since, until=until)
            if games:
                embed.add_field(
                    name="Во что играли",
                    value="\n".join(
                        f"**{game.name}** — {format_duration(game.seconds)}" for game in games
                    ),
                    inline=False,
                )

        embed.set_footer(text=f"Сутки считаются по {self.config.timezone_name}")
        return embed
