"""Текстовые команды статистики.

Оставлены для совместимости с MVP и как запасной вариант, если слэш-команды
не синхронизировались. Вся логика — в `StatsService`.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from ..stats import StatsService, UnknownArgument, resolve_metric, resolve_period


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.stats: StatsService = bot.stats

    @commands.command(name="top", aliases=["leaderboard", "лидеры"])
    @commands.guild_only()
    async def top(
        self,
        ctx: commands.Context,
        metric: str = "combined",
        period: str = "all",
        limit: int = 10,
    ) -> None:
        """Топ активных участников.

        Примеры: `!top`, `!top voice week`, `!top text month 15`
        """
        try:
            metric_key = resolve_metric(metric)
            period_key = resolve_period(period)
        except UnknownArgument as exc:
            await ctx.reply(f"Не понял аргументы: {exc}", mention_author=False)
            return

        embed = await self.stats.leaderboard_embed(ctx.guild, metric_key, period_key, limit)
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="stats", aliases=["me", "стата"])
    @commands.guild_only()
    async def stats_command(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        period: str = "all",
    ) -> None:
        """Статистика участника. Примеры: `!stats`, `!stats @user week`"""
        try:
            period_key = resolve_period(period)
        except UnknownArgument as exc:
            await ctx.reply(f"Не понял аргументы: {exc}", mention_author=False)
            return

        embed = await self.stats.user_embed(ctx.guild, member or ctx.author, period_key)
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="heatmap", aliases=["карта"])
    @commands.guild_only()
    async def heatmap(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Тепловая карта активности. Примеры: `!heatmap`, `!heatmap @user`"""
        async with ctx.typing():
            image, error = await self.stats.heatmap_image(ctx.guild, member)
        if image is None:
            await ctx.reply(error, mention_author=False)
            return
        await ctx.reply(file=image, mention_author=False)

    @commands.command(name="games", aliases=["игры"])
    @commands.guild_only()
    async def games(self, ctx: commands.Context, period: str = "all", limit: int = 10) -> None:
        """Топ игр на сервере. Примеры: `!games`, `!games week`"""
        try:
            period_key = resolve_period(period)
        except UnknownArgument as exc:
            await ctx.reply(f"Не понял аргументы: {exc}", mention_author=False)
            return

        embed = await self.stats.games_embed(ctx.guild, period_key, limit)
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="profile", aliases=["card", "профиль"])
    @commands.guild_only()
    async def profile(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Карточка активности картинкой. Примеры: `!profile`, `!profile @user`"""
        async with ctx.typing():
            card = await self.stats.profile_card(ctx.guild, member or ctx.author)
        await ctx.reply(file=card, mention_author=False)

    @commands.command(name="serverstats", aliases=["сервер"])
    @commands.guild_only()
    async def serverstats(self, ctx: commands.Context, period: str = "all") -> None:
        """Сводка по серверу. Примеры: `!serverstats`, `!serverstats week`"""
        try:
            period_key = resolve_period(period)
        except UnknownArgument as exc:
            await ctx.reply(f"Не понял аргументы: {exc}", mention_author=False)
            return

        embed = await self.stats.server_embed(ctx.guild, period_key)
        await ctx.reply(embed=embed, mention_author=False)
