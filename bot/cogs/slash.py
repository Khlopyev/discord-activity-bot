"""Слэш-команды (раздел 4 брифа: стандарт де-факто, этап v2).

Логика общая с текстовыми командами — обе обёртки зовут `StatsService`.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..stats import MAX_LIMIT, StatsService

log = logging.getLogger(__name__)

METRIC_CHOICES = [
    app_commands.Choice(name="Голосовые каналы", value="voice"),
    app_commands.Choice(name="Сообщения", value="text"),
    app_commands.Choice(name="Общий рейтинг", value="combined"),
]

PERIOD_CHOICES = [
    app_commands.Choice(name="Сегодня", value="day"),
    app_commands.Choice(name="Неделя", value="week"),
    app_commands.Choice(name="Месяц", value="month"),
    app_commands.Choice(name="Всё время", value="all"),
]


class SlashCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.stats: StatsService = bot.stats

    @app_commands.command(name="leaderboard", description="Топ активных участников сервера")
    @app_commands.describe(
        metric="Что считаем", period="За какой период", limit="Сколько мест показать (1-25)"
    )
    @app_commands.choices(metric=METRIC_CHOICES, period=PERIOD_CHOICES)
    @app_commands.guild_only()
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str] | None = None,
        period: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, MAX_LIMIT] = 10,
    ) -> None:
        embed = await self.stats.leaderboard_embed(
            interaction.guild,
            metric.value if metric else "combined",
            period.value if period else "all",
            limit,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stats", description="Статистика активности участника")
    @app_commands.describe(user="Чью статистику показать", period="За какой период")
    @app_commands.choices(period=PERIOD_CHOICES)
    @app_commands.guild_only()
    async def stats_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        embed = await self.stats.user_embed(
            interaction.guild, user or interaction.user, period.value if period else "all"
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="heatmap", description="Тепловая карта активности по дням недели и часам"
    )
    @app_commands.describe(user="Чью карту показать. Без него — по всему серверу")
    @app_commands.guild_only()
    async def heatmap(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        await interaction.response.defer()
        image, error = await self.stats.heatmap_image(interaction.guild, user)
        if image is None:
            await interaction.followup.send(error)
            return
        await interaction.followup.send(file=image)

    @app_commands.command(name="games", description="Во что чаще всего играют на сервере")
    @app_commands.describe(period="За какой период", limit="Сколько игр показать (1-25)")
    @app_commands.choices(period=PERIOD_CHOICES)
    @app_commands.guild_only()
    async def games(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, MAX_LIMIT] = 10,
    ) -> None:
        # Топ игр читает всю таблицу игровых событий гильдии. Даже с
        # индексами на двухлетней истории это сотни миллисекунд, а на сервере
        # медленнее, чем на машине разработчика: в три секунды Discord лучше
        # не упираться.
        await interaction.response.defer()
        embed = await self.stats.games_embed(
            interaction.guild, period.value if period else "all", limit
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="profile", description="Карточка активности участника")
    @app_commands.describe(user="Чью карточку показать")
    @app_commands.guild_only()
    async def profile(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        # Рендер и скачивание аватара занимают заметное время — отвечаем отложенно,
        # иначе Discord закроет взаимодействие по трёхсекундному таймауту.
        await interaction.response.defer()
        card = await self.stats.profile_card(interaction.guild, user or interaction.user)
        await interaction.followup.send(file=card)

    @app_commands.command(name="serverstats", description="Общая сводка по серверу")
    @app_commands.describe(period="За какой период")
    @app_commands.choices(period=PERIOD_CHOICES)
    @app_commands.guild_only()
    async def serverstats(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        # Самая тяжёлая команда: сводка складывается из полудюжины запросов,
        # каждый из которых читает гильдию целиком.
        await interaction.response.defer()
        embed = await self.stats.server_embed(
            interaction.guild, period.value if period else "all"
        )
        await interaction.followup.send(embed=embed)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("Ошибка слэш-команды", exc_info=error)
        message = "Что-то пошло не так, подробности в логах."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
