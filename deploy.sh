#!/usr/bin/env bash
# Развёртывание на сервере: подтянуть код и пересобрать контейнер.
# Запускается на VPS — вручную или из GitHub Actions по SSH.
set -euo pipefail

cd "$(dirname "$0")"

if [ -d .git ]; then
    echo "==> Обновляю код"
    git fetch --prune origin
    git reset --hard origin/main
fi

if [ ! -f .env ]; then
    echo "ОШИБКА: нет файла .env — создайте его из .env.example и впишите токен." >&2
    exit 1
fi

echo "==> Пересобираю и перезапускаю"
docker compose up -d --build

echo "==> Убираю неиспользуемые образы"
docker image prune -f >/dev/null

echo "==> Жду, пока бот войдёт в Discord"
# Контейнер может подниматься несколько секунд; ищем строку входа в логах.
for _ in $(seq 1 30); do
    if docker compose logs --tail 200 2>/dev/null | grep -q "Вошли как"; then
        echo "==> Готово"
        docker compose logs --tail 15
        exit 0
    fi
    sleep 2
done

echo "ПРЕДУПРЕЖДЕНИЕ: за минуту бот так и не вошёл. Последние логи:" >&2
docker compose logs --tail 40 >&2
exit 1
