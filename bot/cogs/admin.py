"""Приватность и администрирование (раздел 6 брифа, этап v5).

`/optout` и `/optin` доступны всем — это управление собственными данными.
Группа `/admin` требует права «Управлять сервером»: Discord прячет такие
команды от остальных сам, но проверка продублирована на стороне бота, потому
что права на команду можно переопределить в настройках сервера.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile

import discord
from discord import app_commands
from discord.ext import commands

from ..db import EXPORTABLE_TABLES
from ..settings import EXCLUDED_CHANNELS, SUMMARY_CHANNEL, SUMMARY_MONTHLY, SUMMARY_WEEKLY

log = logging.getLogger(__name__)

# Discord отклоняет вложения больше лимита сервера; берём консервативный порог.
MAX_UPLOAD_BYTES = 7 * 1024 * 1024


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        self.settings = bot.settings
        self.stats = bot.stats

    # --- приватность, доступно всем ---

    @app_commands.command(
        name="optout", description="Отказаться от сбора статистики и удалить свою историю"
    )
    @app_commands.guild_only()
    async def optout(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        await interaction.response.defer(ephemeral=True)
        await self.settings.set_optout(guild_id, user_id, True)
        # Закрываем то, что уже открыто, иначе фоновая задача досчитает время.
        await self._drop_open_sessions(interaction.user)
        removed = await self.db.purge_user(guild_id, user_id)
        # Иначе лидерборд ещё LEADERBOARD_CACHE_TTL секунд показывает того,
        # кому только что ответили, что его данные удалены.
        self.stats.invalidate(guild_id)

        await interaction.followup.send(
            "Готово. Сбор данных о вас на этом сервере остановлен, "
            f"вся накопленная статистика удалена (записей: {removed}).\n"
            "Вернуть историю нельзя. Возобновить сбор — командой `/optin`.",
            ephemeral=True,
        )

    @app_commands.command(name="optin", description="Возобновить сбор статистики")
    @app_commands.guild_only()
    async def optin(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        if not self.settings.is_opted_out(guild_id, interaction.user.id):
            await interaction.response.send_message(
                "Сбор статистики о вас и так включён.", ephemeral=True
            )
            return

        await self.settings.set_optout(guild_id, interaction.user.id, False)
        await interaction.response.send_message(
            "Сбор статистики возобновлён — с этого момента. "
            "Удалённая ранее история не восстанавливается.",
            ephemeral=True,
        )

    async def _drop_open_sessions(self, member: discord.abc.User) -> None:
        voice = self.bot.get_cog("VoiceTracker")
        if voice is not None and isinstance(member, discord.Member):
            await voice.apply_state(member, None)
        presence = self.bot.get_cog("PresenceTracker")
        if presence is not None and isinstance(member, discord.Member):
            await presence.apply_activity(member)

    # --- админ-группа ---

    admin = app_commands.Group(
        name="admin",
        description="Администрирование бота статистики",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @admin.command(name="exclude-channel", description="Исключить канал из трекинга")
    @app_commands.describe(channel="Канал, который перестанет учитываться")
    async def exclude_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | discord.TextChannel | discord.StageChannel,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return
        added = await self.settings.exclude_channel(interaction.guild.id, channel.id)
        if added:
            await interaction.response.send_message(
                f"{channel.mention} исключён из трекинга. Уже накопленная по нему "
                "статистика сохраняется — учёт останавливается с этого момента.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{channel.mention} уже был исключён.", ephemeral=True
            )

    @admin.command(name="include-channel", description="Вернуть канал в трекинг")
    @app_commands.describe(channel="Канал, который снова начнёт учитываться")
    async def include_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | discord.TextChannel | discord.StageChannel,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return
        removed = await self.settings.include_channel(interaction.guild.id, channel.id)
        message = (
            f"{channel.mention} снова учитывается."
            if removed
            else f"{channel.mention} и так не был исключён."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @admin.command(name="exclusions", description="Показать исключённые каналы")
    async def exclusions(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_admin(interaction):
            return
        guild = interaction.guild
        settings = await self.settings.get(guild.id)
        ids = settings.get(EXCLUDED_CHANNELS, [])

        lines = []
        if guild.afk_channel is not None and self.bot.config.exclude_afk_channel:
            lines.append(f"{guild.afk_channel.mention} — AFK-канал, исключён автоматически")
        for channel_id in self.bot.config.excluded_channel_ids:
            lines.append(f"<#{channel_id}> — из переменной окружения")
        for channel_id in ids:
            lines.append(f"<#{channel_id}> — командой")

        await interaction.response.send_message(
            "\n".join(lines) if lines else "Исключений нет.", ephemeral=True
        )

    @admin.command(name="reset-stats", description="Сбросить статистику сервера или участника")
    @app_commands.describe(
        user="Чью статистику сбросить. Без него сбрасывается весь сервер",
        confirm="Обязательное подтверждение — действие необратимо",
    )
    async def reset_stats(
        self,
        interaction: discord.Interaction,
        confirm: bool,
        user: discord.Member | None = None,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return
        if not confirm:
            await interaction.response.send_message(
                "Сброс отменён: параметр `confirm` должен быть `True`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        if user is not None:
            await self._drop_open_sessions(user)
            removed = await self.db.purge_user(interaction.guild.id, user.id)
            target = user.display_name
        else:
            removed = await self.db.purge_guild(interaction.guild.id)
            target = "весь сервер"
            # Открытые сессии переоткроются на следующем on_ready; чтобы не
            # дописать время в только что очищенные таблицы, сбрасываем их сразу.
            for cog_name in ("VoiceTracker", "PresenceTracker"):
                cog = self.bot.get_cog(cog_name)
                if cog is not None:
                    await cog.reconcile()

        self.stats.invalidate(interaction.guild.id)

        await interaction.followup.send(
            f"Статистика сброшена ({target}). Удалено записей: {removed}.", ephemeral=True
        )

    @admin.command(name="export", description="Выгрузить статистику сервера файлом")
    @app_commands.describe(fmt="Формат выгрузки")
    @app_commands.choices(
        fmt=[
            app_commands.Choice(name="CSV (архив по таблицам)", value="csv"),
            app_commands.Choice(name="JSON (один файл)", value="json"),
        ]
    )
    async def export(
        self, interaction: discord.Interaction, fmt: app_commands.Choice[str] | None = None
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        chosen = fmt.value if fmt else "csv"
        guild = interaction.guild

        tables = {}
        for table in EXPORTABLE_TABLES:
            headers, rows = await self.db.export_rows(guild.id, table)
            tables[table] = (headers, rows)

        if chosen == "json":
            payload = {
                table: [dict(zip(headers, row)) for row in rows]
                for table, (headers, rows) in tables.items()
            }
            blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            filename = f"activity-{guild.id}.json"
        else:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for table, (headers, rows) in tables.items():
                    text = io.StringIO()
                    writer = csv.writer(text, lineterminator="\n")
                    writer.writerow(headers)
                    writer.writerows(rows)
                    # BOM, иначе Excel открывает кириллицу как мусор.
                    archive.writestr(f"{table}.csv", "﻿" + text.getvalue())
            blob = buffer.getvalue()
            filename = f"activity-{guild.id}.zip"

        if len(blob) > MAX_UPLOAD_BYTES:
            await interaction.followup.send(
                f"Выгрузка получилась {len(blob) // 1024 // 1024} МБ — это больше лимита "
                "вложений Discord. Заберите файл базы `data/activity.db` с сервера напрямую.",
                ephemeral=True,
            )
            return

        total = sum(len(rows) for _headers, rows in tables.values())
        await interaction.followup.send(
            f"Выгружено строк: {total}.",
            file=discord.File(io.BytesIO(blob), filename=filename),
            ephemeral=True,
        )

    @admin.command(name="summary-channel", description="Канал для автоматических сводок")
    @app_commands.describe(
        channel="Куда присылать сводки. Без него автосводки выключаются",
        weekly="Присылать еженедельную сводку",
        monthly="Присылать ежемесячную сводку",
    )
    async def summary_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        weekly: bool = True,
        monthly: bool = True,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        if channel is None:
            await self.settings.update(interaction.guild.id, {SUMMARY_CHANNEL: None})
            await interaction.response.send_message("Автосводки выключены.", ephemeral=True)
            return

        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await interaction.response.send_message(
                f"У бота нет прав писать в {channel.mention} "
                "(нужны «Отправлять сообщения» и «Встраивать ссылки»).",
                ephemeral=True,
            )
            return

        await self.settings.update(
            interaction.guild.id,
            {SUMMARY_CHANNEL: channel.id, SUMMARY_WEEKLY: weekly, SUMMARY_MONTHLY: monthly},
        )
        kinds = [k for k, on in (("еженедельная", weekly), ("ежемесячная", monthly)) if on]
        await interaction.response.send_message(
            f"Сводки будут приходить в {channel.mention}: "
            + (", ".join(kinds) if kinds else "ни одной — обе выключены"),
            ephemeral=True,
        )

    # --- общее ---

    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        """Права на слэш-команды можно переопределить в настройках сервера,
        поэтому проверяем сами, а не только через default_permissions."""
        permissions = interaction.user.guild_permissions
        if permissions.manage_guild or permissions.administrator:
            return True
        await interaction.response.send_message(
            "Команда доступна только тем, у кого есть право «Управлять сервером».",
            ephemeral=True,
        )
        return False
