<#
.SYNOPSIS
    Развёртывание с этой машины на VPS одной командой — без GitHub и CI.

.DESCRIPTION
    Копирует исходники по scp и пересобирает контейнер на сервере.
    Не трогает .env и базу данных на сервере: токен и статистика остаются.

.EXAMPLE
    .\scripts\deploy-local.ps1 -Server bot@203.0.113.10

.EXAMPLE
    # Первый раз — вместе с накопленной локально статистикой
    .\scripts\deploy-local.ps1 -Server bot@203.0.113.10 -WithDatabase
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,

    [string]$Path = "~/discord-acb",

    # Перенести локальную базу на сервер, перезаписав тамошнюю.
    [switch]$WithDatabase
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> Проверяю тесты перед отправкой" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) {
    throw "Тесты не прошли — развёртывание отменено."
}

Write-Host "==> Готовлю каталоги на сервере" -ForegroundColor Cyan
ssh $Server "mkdir -p $Path/data"

Write-Host "==> Копирую исходники" -ForegroundColor Cyan
$items = @("bot", "tools", "assets", "tests")
foreach ($item in $items) {
    scp -r -q $item "${Server}:${Path}/"
}

$files = @(
    "Dockerfile", "docker-compose.yml", "deploy.sh",
    "requirements.txt", "requirements-dev.txt", "pytest.ini",
    "README.md", "DEPLOY.md", ".env.example", ".gitignore"
)
scp -q $files "${Server}:${Path}/"

if ($WithDatabase) {
    Write-Host "==> Переношу базу (убедитесь, что локальный бот остановлен)" -ForegroundColor Yellow
    ssh $Server "cd $Path && docker compose stop 2>/dev/null || true"
    scp -q "data\activity.db" "${Server}:${Path}/data/"
    # WAL и SHM переносим, только если они есть: иначе scp завершится ошибкой.
    foreach ($suffix in @("-wal", "-shm")) {
        $sidecar = "data\activity.db$suffix"
        if (Test-Path $sidecar) { scp -q $sidecar "${Server}:${Path}/data/" }
    }
}

Write-Host "==> Пересобираю на сервере" -ForegroundColor Cyan
ssh $Server "cd $Path && chmod +x deploy.sh && ./deploy.sh"

Write-Host "==> Развёрнуто" -ForegroundColor Green
