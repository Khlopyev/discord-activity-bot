"""Сборка ответов со статистикой — общий слой для текстовых и слэш-команд.

Команды остаются тонкими: они только разбирают аргументы и отдают эмбед,
собранный здесь. Благодаря этому `!top voice week` и `/leaderboard` дают
идентичный результат.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from io import BytesIO

import discord

from .card import CHART_DAYS, CardData, render_card
from .config import Config
from .heatmap import HeatmapData, render_heatmap
from .db import Database, GuildTotals, LeaderboardRow
from .timeutil import (
    ISO_DATE,
    PERIOD_TITLES,
    WEEKDAY_NAMES_SQLITE,
    format_duration,
    format_hour_range,
    local_date,
    period_start,
    utcnow,
)

log = logging.getLogger(__name__)

MAX_LIMIT = 25
MEDALS = ("🥇", "🥈", "🥉")

METRIC_ALIASES = {
    "voice": "voice", "войс": "voice", "голос": "voice", "v": "voice",
    "text": "text", "текст": "text", "msg": "text", "messages": "text", "t": "text",
    "combined": "combined", "all": "combined", "общий": "combined", "c": "combined",
}

PERIOD_ALIASES = {
    "day": "day", "today": "day", "день": "day", "сегодня": "day", "d": "day",
    "week": "week", "неделя": "week", "нед": "week", "w": "week",
    "month": "month", "месяц": "month", "мес": "month", "m": "month",
    "all": "all", "alltime": "all", "всё": "all", "все": "all", "a": "all",
}

METRIC_TITLES = {
    "voice": "по времени в голосовых каналах",
    "text": "по количеству сообщений",
    "combined": "по общему рейтингу активности",
}

EMPTY_HINT = "Данных пока нет — статистика копится с момента запуска бота."

# Раздел 3.3 брифа прямо требует обозначать неполноту presence-данных в интерфейсе.
PRESENCE_CAVEAT = "у скрывших активность в настройках приватности игры не видны"


class UnknownArgument(ValueError):
    """Пользователь передал неизвестную метрику или период."""


def resolve_metric(raw: str) -> str:
    key = METRIC_ALIASES.get(raw.strip().lower())
    if key is None:
        raise UnknownArgument(
            f"неизвестная метрика `{raw}`. Доступны: `voice`, `text`, `combined`"
        )
    return key


def resolve_period(raw: str) -> str:
    key = PERIOD_ALIASES.get(raw.strip().lower())
    if key is None:
        raise UnknownArgument(
            f"неизвестный период `{raw}`. Доступны: `day`, `week`, `month`, `all`"
        )
    return key


class StatsService:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        # Кэш лидербордов и сводок (раздел 7 брифа).
        self._cache: dict[tuple, tuple[float, object]] = {}

    # --- вспомогательное ---

    @property
    def _weights(self) -> dict[str, float]:
        return {
            "voice_weight": self.config.combined_voice_weight,
            "message_weight": self.config.combined_message_weight,
        }

    def since(self, period: str) -> str | None:
        today = local_date(utcnow(), self.config.timezone)
        return period_start(today, period)

    async def _cached(self, key: tuple, factory):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = await factory()
        if self.config.leaderboard_cache_ttl > 0:
            self._cache[key] = (now + self.config.leaderboard_cache_ttl, value)
        return value

    def invalidate(self, guild_id: int) -> None:
        """Забыть всё, что закэшировано по гильдии.

        Вызывается после удаления данных. Само по себе устаревание по TTL для
        этого не годится: после `/optout` человек получает ответ, что его
        история удалена, и всё это время продолжает висеть в лидерборде —
        ровно то, от чего он отказался.

        Ключи кэша строятся как (вид, guild_id, ...), поэтому гильдия
        определяется вторым элементом.
        """
        now = time.monotonic()
        self._cache = {
            key: entry
            for key, entry in self._cache.items()
            # Заодно выбрасываем протухшее: раньше записи оставались в словаре
            # навсегда, TTL лишь помечал их негодными при чтении.
            if key[1] != guild_id and entry[0] > now
        }

    def _footer(self, period: str, *, with_formula: bool) -> str:
        base = f"{PERIOD_TITLES[period].capitalize()} · сутки по {self.config.timezone_name}"
        if with_formula:
            base += (
                f" · формула: минуты×{self.config.combined_voice_weight:g}"
                f" + сообщения×{self.config.combined_message_weight:g}"
            )
        return base

    @staticmethod
    def display_name(guild: discord.Guild, row: LeaderboardRow) -> str:
        member = guild.get_member(row.user_id)
        if member is not None:
            return member.display_name
        return row.username or f"ID {row.user_id}"

    @staticmethod
    def _channel_label(guild: discord.Guild, channel_id: int) -> str:
        channel = guild.get_channel(channel_id)
        if channel is not None:
            return channel.mention
        return f"удалённый канал ({channel_id})"

    # --- лидерборд ---

    async def leaderboard_embed(
        self, guild: discord.Guild, metric: str, period: str, limit: int
    ) -> discord.Embed:
        limit = max(1, min(limit, MAX_LIMIT))
        rows: list[LeaderboardRow] = await self._cached(
            ("top", guild.id, metric, period, limit),
            lambda: self.db.leaderboard(
                guild.id, metric, limit=limit, since=self.since(period), **self._weights
            ),
        )

        # В заголовке — сколько мест реально показано, а не сколько запрошено:
        # «Топ-25» при двенадцати строках выглядит как потерянные данные.
        embed = discord.Embed(
            title=f"Топ-{len(rows) or limit} активных — {METRIC_TITLES[metric]}",
            colour=discord.Colour.blurple(),
        )
        if not rows:
            embed.description = EMPTY_HINT
            embed.set_footer(text=self._footer(period, with_formula=False))
            return embed

        lines = []
        for index, row in enumerate(rows, start=1):
            prefix = MEDALS[index - 1] if index <= len(MEDALS) else f"`{index:>2}.`"
            name = discord.utils.escape_markdown(self.display_name(guild, row))
            lines.append(f"{prefix} **{name}** — {self._metric_value(metric, row)}")

        embed.description = "\n".join(lines)
        embed.set_footer(text=self._footer(period, with_formula=metric == "combined"))
        return embed

    @staticmethod
    def _metric_value(metric: str, row: LeaderboardRow) -> str:
        if metric == "voice":
            value = format_duration(row.voice_seconds)
            if row.stream_seconds:
                value += f" (из них стрим {format_duration(row.stream_seconds)})"
            return value
        if metric == "text":
            return f"{row.message_count} сообщ."
        return (
            f"{row.combined_score:.0f} очк. "
            f"({format_duration(row.voice_seconds)} · {row.message_count} сообщ.)"
        )

    # --- профиль участника ---

    async def user_embed(
        self, guild: discord.Guild, member: discord.abc.User, period: str
    ) -> discord.Embed:
        since = self.since(period)
        totals = await self.db.user_totals(guild.id, member.id, since=since, **self._weights)

        embed = discord.Embed(
            title=f"Активность — {member.display_name}", colour=discord.Colour.blurple()
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if totals is None:
            embed.description = f"{PERIOD_TITLES[period].capitalize()} данных нет."
            embed.set_footer(text=self._footer(period, with_formula=False))
            return embed

        embed.add_field(name="В голосовых", value=format_duration(totals.voice_seconds))
        embed.add_field(name="Сообщений", value=str(totals.message_count))
        embed.add_field(name="Рейтинг", value=f"{totals.combined_score:.0f} очк.")
        if totals.stream_seconds:
            embed.add_field(name="Стримил в войсе", value=format_duration(totals.stream_seconds))

        rank = await self.db.user_rank(guild.id, member.id, since=since, **self._weights)
        if rank is not None:
            embed.add_field(name="Место на сервере", value=f"{rank[0]} из {rank[1]}")

        embed.set_footer(text=self._footer(period, with_formula=True))
        return embed

    # --- карточка профиля (раздел 3.5) ---

    async def profile_card(
        self, guild: discord.Guild, member: discord.Member | discord.abc.User
    ) -> discord.File:
        """Собрать карточку-изображение. Рендер уходит в поток — Pillow блокирующий."""
        weights = self._weights
        totals_all = await self.db.user_totals(guild.id, member.id, **weights)
        totals_week = await self.db.user_totals(
            guild.id, member.id, since=self.since("week"), **weights
        )
        totals_month = await self.db.user_totals(
            guild.id, member.id, since=self.since("month"), **weights
        )
        rank = await self.db.user_rank(guild.id, member.id, **weights)

        today = local_date(utcnow(), self.config.timezone)
        first_day = today - timedelta(days=CHART_DAYS - 1)
        points = await self.db.user_daily_series(
            guild.id, member.id, since=first_day.strftime(ISO_DATE), **weights
        )
        series = []
        for offset in range(CHART_DAYS):
            day = first_day + timedelta(days=offset)
            point = points.get(day.strftime(ISO_DATE))
            series.append((str(day.day), point.score if point else 0.0))

        favourite = await self.db.user_favorite_voice_channel(guild.id, member.id)
        favourite_label = None
        if favourite is not None:
            channel = guild.get_channel(favourite[0])
            favourite_label = channel.name if channel is not None else None

        top_game = None
        if self.config.enable_presence_tracking:
            game = await self.db.user_top_game(guild.id, member.id)
            if game is not None:
                top_game = (game.name, game.seconds)

        slot = await self.db.busiest_slot(guild.id, user_id=member.id, **weights)
        peak = None
        if slot is not None:
            weekday, hour = slot
            peak = f"{WEEKDAY_NAMES_SQLITE[weekday]}, {format_hour_range(hour)}"

        data = CardData(
            display_name=member.display_name,
            guild_name=guild.name,
            avatar_png=await self._avatar_bytes(member),
            rank=rank[0] if rank else None,
            rank_total=rank[1] if rank else None,
            score=totals_all.combined_score if totals_all else 0.0,
            voice_all=totals_all.voice_seconds if totals_all else 0,
            voice_week=totals_week.voice_seconds if totals_week else 0,
            voice_month=totals_month.voice_seconds if totals_month else 0,
            messages_all=totals_all.message_count if totals_all else 0,
            messages_week=totals_week.message_count if totals_week else 0,
            messages_month=totals_month.message_count if totals_month else 0,
            stream_all=totals_all.stream_seconds if totals_all else 0,
            favourite_channel=favourite_label,
            top_game=top_game,
            peak_slot=peak,
            series=series,
            footer=f"За всё время · сутки по {self.config.timezone_name}",
        )

        png = await asyncio.to_thread(render_card, data)
        return discord.File(BytesIO(png), filename=f"profile-{member.id}.png")

    @staticmethod
    async def _avatar_bytes(member: discord.abc.User) -> bytes | None:
        try:
            asset = member.display_avatar.replace(size=256, format="png")
            return await asset.read()
        except (discord.HTTPException, discord.NotFound, ValueError):
            log.warning("Не удалось скачать аватар %s, будет заглушка", member.id)
            return None

    # --- тепловая карта (раздел 3.2) ---

    async def heatmap_image(
        self, guild: discord.Guild, member: discord.Member | None = None
    ) -> tuple[discord.File | None, str | None]:
        """Изображение тепловой карты. Второй элемент — текст ошибки, если данных нет."""
        user_id = member.id if member is not None else None
        slots = await self.db.heatmap_grid(guild.id, user_id=user_id, **self._weights)
        if not slots:
            who = f"по {member.display_name}" if member else "по серверу"
            return None, f"Данных {who} пока нет — тепловая карта копится с момента запуска."

        if member is not None:
            title = f"Тепловая карта — {member.display_name}"
            subtitle = "Когда участник активен: строки — дни недели, столбцы — часы"
        else:
            title = f"Тепловая карта — {guild.name}"
            subtitle = "Когда сервер живёт: строки — дни недели, столбцы — часы"

        data = HeatmapData(
            title=title,
            subtitle=subtitle,
            slots=slots,
            footer=f"За всё время · часы по {self.config.timezone_name}",
        )
        png = await asyncio.to_thread(render_heatmap, data)
        suffix = member.id if member else guild.id
        return discord.File(BytesIO(png), filename=f"heatmap-{suffix}.png"), None

    # --- игры (раздел 3.3) ---

    async def games_embed(self, guild: discord.Guild, period: str, limit: int = 10) -> discord.Embed:
        if not self.config.enable_presence_tracking:
            return discord.Embed(
                title="Трекинг игр выключен",
                description=(
                    "Включается через `ENABLE_PRESENCE_TRACKING=true` в `.env` и "
                    "привилегированные intents Presence и Server Members "
                    "в Developer Portal."
                ),
                colour=discord.Colour.blurple(),
            )

        limit = max(1, min(limit, MAX_LIMIT))
        rows = await self._cached(
            ("games", guild.id, period, limit),
            lambda: self.db.top_games(guild.id, limit=limit, since=self.since(period)),
        )

        embed = discord.Embed(
            title=f"Топ-{len(rows) or limit} игр на сервере", colour=discord.Colour.blurple()
        )
        if not rows:
            embed.description = EMPTY_HINT
            embed.set_footer(text=self._footer(period, with_formula=False))
            return embed

        lines = []
        for index, row in enumerate(rows, start=1):
            prefix = MEDALS[index - 1] if index <= len(MEDALS) else f"`{index:>2}.`"
            name = discord.utils.escape_markdown(row.name)
            lines.append(
                f"{prefix} **{name}** — {format_duration(row.seconds)}"
                f" · игроков: {row.players}"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(text=self._footer(period, with_formula=False) + " · " + PRESENCE_CAVEAT)
        return embed

    # --- сводка по серверу ---

    async def server_embed(self, guild: discord.Guild, period: str) -> discord.Embed:
        since = self.since(period)
        totals: GuildTotals = await self._cached(
            ("server", guild.id, period), lambda: self.db.guild_totals(guild.id, since=since)
        )

        embed = discord.Embed(
            title=f"Статистика сервера — {guild.name}", colour=discord.Colour.blurple()
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)

        if totals.active_users == 0:
            embed.description = EMPTY_HINT
            embed.set_footer(text=self._footer(period, with_formula=False))
            return embed

        embed.add_field(name="Всего в голосовых", value=format_duration(totals.voice_seconds))
        embed.add_field(name="Сообщений", value=str(totals.message_count))
        embed.add_field(name="Активных участников", value=str(totals.active_users))
        if totals.stream_seconds:
            embed.add_field(name="Стримов в войсе", value=format_duration(totals.stream_seconds))

        voice_channels = await self.db.top_voice_channels(guild.id, since=since)
        if voice_channels:
            embed.add_field(
                name="Популярные голосовые",
                value="\n".join(
                    f"{self._channel_label(guild, cid)} — {format_duration(seconds)}"
                    for cid, seconds in voice_channels
                ),
                inline=False,
            )

        text_channels = await self.db.top_text_channels(guild.id, since=since)
        if text_channels:
            embed.add_field(
                name="Популярные текстовые",
                value="\n".join(
                    f"{self._channel_label(guild, cid)} — {count} сообщ."
                    for cid, count in text_channels
                ),
                inline=False,
            )

        weekday = await self.db.busiest_weekday(guild.id, since=since, **self._weights)
        hour = await self.db.busiest_hour(guild.id, **self._weights)
        rhythm = []
        if weekday is not None:
            rhythm.append(f"День недели: **{WEEKDAY_NAMES_SQLITE[weekday]}**")
        if hour is not None:
            # Тепловая карта не хранит даты, поэтому час всегда за всё время.
            rhythm.append(f"Час суток: **{format_hour_range(hour)}** (за всё время)")
        if rhythm:
            embed.add_field(name="Пик активности", value="\n".join(rhythm), inline=False)

        embed.set_footer(text=self._footer(period, with_formula=False))
        return embed
