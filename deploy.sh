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
# Контейнер поднимается несколько секунд, но первый запуск после обновления
# схемы дольше: бот достраивает индексы и собирает статистику планировщика до
# того, как войдёт в Discord. На базе в пару гигабайт это десятки секунд, а на
# небыстром диске VPS — заметно больше, поэтому запас взят с избытком.
WAIT_ATTEMPTS=150
WAIT_STEP=2
for _ in $(seq 1 "$WAIT_ATTEMPTS"); do
    if docker compose logs --tail 200 2>/dev/null | grep -q "Вошли как"; then
        echo "==> Готово"
        docker compose logs --tail 15
        exit 0
    fi
    sleep "$WAIT_STEP"
done

echo "ОШИБКА: за $((WAIT_ATTEMPTS * WAIT_STEP / 60)) мин бот так и не вошёл в Discord." >&2
echo "Последние логи:" >&2
docker compose logs --tail 40 >&2
exit 1
