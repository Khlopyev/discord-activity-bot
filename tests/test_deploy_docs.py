"""Список файлов для переноса на сервер живёт в двух местах.

DEPLOY.md диктует его человеку, scripts/deploy-local.ps1 — машине. Разойтись
им нельзя: пропущенный в инструкции файл человек не заметит, пока выкат не
упрётся в его отсутствие уже на сервере. Ровно так из инструкции и выпали
deploy.sh и .env.example.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_MD = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
DEPLOY_PS1 = (ROOT / "scripts" / "deploy-local.ps1").read_text(encoding="utf-8")

SERVER = "bot@ВАШ_IP"


def _doc_files() -> list[str]:
    """Файлы из корня: строка `scp A B C bot@ВАШ_IP:...` без -r."""
    line = re.search(rf"PS> scp (?!-r )(.+?) {SERVER}:", DEPLOY_MD)
    assert line, "в DEPLOY.md не нашлась команда scp для файлов из корня"
    return line.group(1).split()


def _doc_dirs() -> list[str]:
    """Каталоги: строка `scp -r discord-acb\bot ... bot@ВАШ_IP:...`."""
    line = re.search(rf"PS> scp -r (.+?) {SERVER}:", DEPLOY_MD)
    assert line, "в DEPLOY.md не нашлась команда scp для каталогов"
    return [item.split("\\")[-1] for item in line.group(1).split()]


def _script_list(name: str) -> list[str]:
    """Содержимое массива $files или $items из deploy-local.ps1."""
    block = re.search(rf"\${name}\s*=\s*@\((.*?)\)", DEPLOY_PS1, re.DOTALL)
    assert block, f"в deploy-local.ps1 не нашёлся массив ${name}"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_file_lists_match():
    assert sorted(_doc_files()) == sorted(_script_list("files"))


def test_directory_lists_match():
    assert sorted(_doc_dirs()) == sorted(_script_list("items"))


@pytest.mark.parametrize("name", _doc_files() + _doc_dirs())
def test_everything_listed_exists(name):
    """Инструкция не должна отправлять на сервер то, чего в репозитории нет."""
    assert (ROOT / name).exists(), f"{name} перечислен в DEPLOY.md, но его нет"


def test_deploy_script_is_made_executable():
    """scp не переносит бит запуска, и без chmod ./deploy.sh не стартует."""
    assert "chmod +x" in DEPLOY_MD, "в DEPLOY.md нет chmod +x для deploy.sh"
    assert "chmod +x deploy.sh" in DEPLOY_PS1


def test_deploy_script_is_shipped():
    """Без deploy.sh на сервере не работает ни один из вариантов выката."""
    assert "deploy.sh" in _doc_files()
    assert "deploy.sh" in _script_list("files")
