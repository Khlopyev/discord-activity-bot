"""Штатная остановка бота (bot/main.py).

Client.run() из discord.py ловит только KeyboardInterrupt, то есть SIGINT.
SIGTERM — а это то, чем `docker compose stop` и любой перезапуск контейнера
останавливают бота — не перехватывался вовсе. В контейнере python работает как
PID 1, для которого ядро не применяет действие по умолчанию, так что
необработанный SIGTERM просто игнорировался: docker ждал весь
stop_grace_period и добивал процесс SIGKILL, теряя незакрытые сессии.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from bot.main import (
    STOP_SIGNALS,
    _serve,
    install_stop_handlers,
    shutdown_trackers,
)


class FakeLoop:
    """Цикл, записывающий, на какие сигналы вешали обработчик."""

    def __init__(self, *, supported: bool = True) -> None:
        self.supported = supported
        self.handlers: dict[int, object] = {}

    def add_signal_handler(self, sig, callback):
        if not self.supported:
            # Так ведёт себя ProactorEventLoop на Windows.
            raise NotImplementedError
        self.handlers[sig] = callback


def test_sigterm_is_among_the_signals_we_watch():
    """Ради него всё и затевалось — он не должен потеряться при правках."""
    assert "SIGTERM" in STOP_SIGNALS


def test_handlers_are_installed_for_every_supported_signal():
    loop = FakeLoop()
    installed = install_stop_handlers(loop, lambda: None)
    expected = [name for name in STOP_SIGNALS if hasattr(signal, name)]
    assert installed == expected
    assert len(loop.handlers) == len(expected)


def test_platform_without_signal_handlers_is_not_a_crash():
    """На Windows add_signal_handler не реализован, и это штатная ситуация."""
    installed = install_stop_handlers(FakeLoop(supported=False), lambda: None)
    assert installed == []


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM на Windows не доставляется")
def test_real_sigterm_triggers_the_callback():
    """Главный тест: настоящий сигнал настоящему циклу.

    До фикса SIGTERM не был подписан ни на что и убивал процесс на месте.
    """

    async def scenario():
        loop = asyncio.get_running_loop()
        fired = asyncio.Event()
        installed = install_stop_handlers(loop, fired.set)
        assert "SIGTERM" in installed
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(fired.wait(), timeout=5)

    # asyncio.run закрывает цикл, а тот снимает обработчики: соседние тесты
    # не унаследуют подписку на SIGTERM.
    asyncio.run(scenario())


class FakeCog:
    def __init__(self, log: list[str], name: str, *, fails: bool = False) -> None:
        self._log = log
        self._name = name
        self._fails = fails

    async def shutdown(self) -> None:
        self._log.append(self._name)
        if self._fails:
            raise RuntimeError("ког не смог остановиться")


class FakeBot:
    def __init__(self, cogs: dict[str, FakeCog]) -> None:
        self._cogs = cogs

    def get_cog(self, name: str):
        return self._cogs.get(name)


@pytest.mark.asyncio
async def test_every_tracker_gets_a_chance_to_close():
    calls: list[str] = []
    bot = FakeBot({
        "VoiceTracker": FakeCog(calls, "voice"),
        "TextTracker": FakeCog(calls, "text"),
        "PresenceTracker": FakeCog(calls, "presence"),
    })
    await shutdown_trackers(bot)
    assert calls == ["voice", "text", "presence"]


@pytest.mark.asyncio
async def test_missing_cog_is_skipped():
    """PresenceTracker не добавляется при ENABLE_PRESENCE_TRACKING=false."""
    calls: list[str] = []
    bot = FakeBot({"VoiceTracker": FakeCog(calls, "voice")})
    await shutdown_trackers(bot)
    assert calls == ["voice"]


@pytest.mark.asyncio
async def test_failing_cog_does_not_block_the_rest():
    """Недобитый трекер — потеря данных, поэтому очередь идёт до конца."""
    calls: list[str] = []
    bot = FakeBot({
        "VoiceTracker": FakeCog(calls, "voice", fails=True),
        "TextTracker": FakeCog(calls, "text"),
    })
    await shutdown_trackers(bot)
    assert calls == ["voice", "text"]


# --- остановка должна доводиться до конца ---


class SlowClosingBot:
    """Заглушка с той же формой, что у настоящего бота.

    start() возвращается сразу после закрытия вебсокета, а закрытие базы идёт
    в той же задаче и уступает управление — как aiosqlite через поток.
    """

    def __init__(self) -> None:
        self.closing = asyncio.Event()
        self.database_closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def start(self, token: str) -> None:
        await self.closing.wait()

    async def close(self) -> None:
        self.closing.set()          # аналог super().close(): вебсокет закрыт
        await asyncio.sleep(0.05)   # аналог db.close() через поток
        self.database_closed = True


def test_shutdown_finishes_before_the_loop_tears_down(monkeypatch):
    """Остановка не должна обрываться на выходе из цикла событий.

    bot.start() возвращается, как только закрыт вебсокет. Если не дождаться
    задачи остановки, asyncio.run отменит её — и до закрытия базы дело не
    дойдёт: SQLite останется с незачекпойнченным WAL, а PRAGMA optimize не
    выполнится. Ровно это и случилось на сервере после первого выката.

    Сигнал не шлём: подменяем установщик обработчиков, чтобы тест шёл и на
    Windows, где SIGTERM не доставляется.
    """

    def fire_immediately(loop, on_stop):
        loop.call_soon(on_stop)
        return ["SIGTERM"]

    monkeypatch.setattr("bot.main.install_stop_handlers", fire_immediately)

    bot = SlowClosingBot()
    asyncio.run(_serve(bot, "токен"))
    assert bot.database_closed, "остановку оборвали до закрытия базы"
