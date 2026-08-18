FROM python:3.12-slim

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
