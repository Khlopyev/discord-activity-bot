# Дайджест, а не только тег: тег 3.12-slim переезжает на новый патч-релиз
# Python и свежие системные пакеты, и пересборка через полгода собрала бы уже
# другой образ. Здесь это Python 3.12.14 на Debian trixie от 2026-08-16.
# Обновление приезжает отдельным PR от Dependabot (.github/dependabot.yml).
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# DejaVu — шрифт для карточки профиля: в slim-образе шрифтов нет вообще,
# и Pillow свалился бы на встроенный растровый.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Данные держим в томе, чтобы статистика переживала пересоздание контейнера.
ENV DATABASE_PATH=/data/activity.db
VOLUME ["/data"]

CMD ["python", "-m", "bot"]
