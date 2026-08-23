"""Отложенный ответ у тяжёлых слэш-команд.

Discord закрывает взаимодействие, если бот не ответил за три секунды. Часть
команд читает таблицу событий гильдии целиком, и на длинной истории это
сотни миллисекунд на машине разработчика — на VPS больше. Такие команды
обязаны сначала подтвердить приём (defer), а отвечать уже в followup.

Лёгкие команды отвечают сразу и намеренно: лишний defer показывает
«бот думает…» там, где ответ мгновенный.
"""

from __future__ import annotations

import pytest

from bot.cogs.slash import SlashCommands


class FakeResponse:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.deferred = False

    async def defer(self, **kwargs) -> None:
        assert not self.deferred, "defer вызван дважды"
        self.deferred = True
        self._log.append("defer")

    async def send_message(self, **kwargs) -> None:
        assert not self.deferred, "после defer отвечать нужно через followup"
        self._log.append("send_message")

    def is_done(self) -> bool:
        return self.deferred


class FakeFollowup:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def send(self, *args, **kwargs) -> None:
        self._log.append("followup")


class FakeInteraction:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)
        self.guild = object()
        self.user = object()


class FakeStats:
    """Отмечает в журнале момент, когда команда полезла за данными."""

    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction

    def _note(self, name: str) -> None:
        self._interaction.log.append(f"query:{name}")

    async def leaderboard_embed(self, *args, **kwargs):
        self._note("leaderboard")
        return "embed"

    async def user_embed(self, *args, **kwargs):
        self._note("user")
        return "embed"

    async def games_embed(self, *args, **kwargs):
        self._note("games")
        return "embed"

    async def server_embed(self, *args, **kwargs):
        self._note("server")
        return "embed"


class FakeBot:
    def __init__(self, stats) -> None:
        self.stats = stats


async def run(command_name: str) -> list[str]:
    interaction = FakeInteraction()
    cog = SlashCommands(FakeBot(FakeStats(interaction)))
    command = getattr(SlashCommands, command_name)
    await command.callback(cog, interaction)
    return interaction.log


@pytest.mark.parametrize(
    "command, query",
    [("serverstats", "server"), ("games", "games")],
)
@pytest.mark.asyncio
async def test_heavy_commands_defer_before_touching_the_database(command, query):
    assert await run(command) == ["defer", f"query:{query}", "followup"]


@pytest.mark.parametrize(
    "command, query",
    [("leaderboard", "leaderboard"), ("stats_command", "user")],
)
@pytest.mark.asyncio
async def test_light_commands_answer_immediately(command, query):
    """Осознанное решение, а не недосмотр: эти запросы укладываются в миллисекунды."""
    assert await run(command) == [f"query:{query}", "send_message"]
