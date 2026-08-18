"""Правила исключения каналов и пользователей из трекинга.

Разделы 3.1 (AFK, боты, исключённые каналы) и 7 (opt-out) брифа.
Исключения приходят из двух источников: неизменяемого списка в окружении и
настроек гильдии, редактируемых админ-командами на лету.
"""

from __future__ import annotations

import discord

from .config import Config
from .settings import SettingsStore


def is_tracked_channel(
    channel: discord.abc.GuildChannel | None,
    config: Config,
    settings: SettingsStore | None = None,
) -> bool:
    """AFK-канал и каналы из исключений не трекаются.

    Для тредов проверяется ещё и родительский канал, иначе исключение
    текстового канала не распространялось бы на его треды.
    """
    if channel is None:
        return False

    guild = getattr(channel, "guild", None)
    excluded = set(config.excluded_channel_ids)
    if settings is not None and guild is not None:
        excluded |= settings.excluded_channels(guild.id)

    if channel.id in excluded:
        return False

    parent_id = getattr(channel, "parent_id", None)
    if parent_id is not None and parent_id in excluded:
        return False

    if config.exclude_afk_channel and guild is not None and guild.afk_channel is not None:
        if channel.id == guild.afk_channel.id:
            return False

    return True


def is_tracked_user(
    guild_id: int, user: discord.abc.User, settings: SettingsStore | None = None
) -> bool:
    """Боты не считаются никогда, отказавшиеся от трекинга — с момента отказа."""
    if user.bot:
        return False
    if settings is not None and settings.is_opted_out(guild_id, user.id):
        return False
    return True
