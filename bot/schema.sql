-- Схема MVP. Соответствует разделу 5 брифа, упрощена под этап 1:
--   * presence_sessions не создаётся (Rich Presence — этап v3);
--   * voice_sessions расширена полями is_open/credited_until, чтобы время
--     начислялось инкрементально и переживало перезапуск бота.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guilds (
    guild_id      INTEGER PRIMARY KEY,
    name          TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    is_bot     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Сырые голосовые сессии. Чистятся по RAW_RETENTION_DAYS, агрегаты остаются.
CREATE TABLE IF NOT EXISTS voice_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    joined_at        TEXT    NOT NULL,
    left_at          TEXT,
    is_stream        INTEGER NOT NULL DEFAULT 0,
    is_open          INTEGER NOT NULL DEFAULT 1,
    -- До какого момента время сессии уже начислено в daily_activity_summary.
    credited_until   TEXT    NOT NULL,
    duration_seconds INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_voice_sessions_open
    ON voice_sessions (guild_id, user_id) WHERE is_open = 1;
CREATE INDEX IF NOT EXISTS idx_voice_sessions_closed
    ON voice_sessions (left_at) WHERE is_open = 0;

-- Отказ от трекинга (раздел 7 брифа, приватность). Хранится по гильдиям:
-- отказ на одном сервере не должен распространяться на другие.
CREATE TABLE IF NOT EXISTS optouts (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Сырые сессии Rich Presence (раздел 5 брифа). Как и voice_sessions, ведутся
-- инкрементально: credited_until защищает от потери данных при падении бота.
CREATE TABLE IF NOT EXISTS presence_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    activity_type    TEXT    NOT NULL,  -- playing / streaming / listening / watching
    activity_name    TEXT    NOT NULL,  -- "Dota 2", "Valorant" и т.д.
    started_at       TEXT    NOT NULL,
    ended_at         TEXT,
    is_open          INTEGER NOT NULL DEFAULT 1,
    credited_until   TEXT    NOT NULL,
    duration_seconds INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_presence_sessions_open
    ON presence_sessions (guild_id, user_id) WHERE is_open = 1;
CREATE INDEX IF NOT EXISTS idx_presence_sessions_closed
    ON presence_sessions (ended_at) WHERE is_open = 0;

-- Посуточный агрегат по играм: сырые presence_sessions чистятся по ретеншну,
-- а статистика «во что играли» должна оставаться.
CREATE TABLE IF NOT EXISTS game_events_daily (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    activity_name TEXT    NOT NULL,
    date          TEXT    NOT NULL,
    seconds       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, activity_name, date)
);

CREATE INDEX IF NOT EXISTS idx_game_events_guild_date
    ON game_events_daily (guild_id, date);

-- Поканальный агрегат голосовой активности. В разделе 5 брифа такой разбивки
-- нет (есть только message_events_daily), но она нужна для «самого популярного
-- голосового канала» из раздела 3.4: сырые voice_sessions чистятся по ретеншну.
CREATE TABLE IF NOT EXISTS voice_events_daily (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    date           TEXT    NOT NULL,
    voice_seconds  INTEGER NOT NULL DEFAULT 0,
    stream_seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, channel_id, date)
);

CREATE INDEX IF NOT EXISTS idx_voice_events_guild_date
    ON voice_events_daily (guild_id, date);

-- Тепловая карта активности (раздел 3.2): 168 строк на сервер, растёт только
-- вширь по числу серверов. Даёт «самый активный час» из раздела 3.4.
CREATE TABLE IF NOT EXISTS activity_heatmap (
    guild_id      INTEGER NOT NULL,
    weekday       INTEGER NOT NULL,  -- нотация strftime('%w'): 0 = воскресенье
    hour          INTEGER NOT NULL,  -- 0-23 в тайм-зоне агрегации
    voice_seconds INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, weekday, hour)
);

-- Та же тепловая карта, но с разбивкой по участникам — для «самого активного
-- времени суток» конкретного человека (раздел 3.5) и персональной тепловой
-- карты. Ограничена 168 строками на участника, поэтому растёт предсказуемо.
--
-- Отдельная таблица, а не колонка в activity_heatmap: та уже накапливает данные,
-- и смена первичного ключа потребовала бы миграции с потерей истории.
CREATE TABLE IF NOT EXISTS user_heatmap (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    weekday       INTEGER NOT NULL,  -- нотация strftime('%w'): 0 = воскресенье
    hour          INTEGER NOT NULL,  -- 0-23 в тайм-зоне агрегации
    voice_seconds INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, weekday, hour)
);

-- Агрегат текстовой активности (сырые сообщения не хранятся: раздел 7, приватность).
CREATE TABLE IF NOT EXISTS message_events_daily (
    guild_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    date              TEXT    NOT NULL,
    message_count     INTEGER NOT NULL DEFAULT 0,
    char_count_approx INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, channel_id, date)
);

-- Предрасчитанный агрегат для лидербордов.
CREATE TABLE IF NOT EXISTS daily_activity_summary (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    date           TEXT    NOT NULL,
    voice_seconds  INTEGER NOT NULL DEFAULT 0,
    message_count  INTEGER NOT NULL DEFAULT 0,
    stream_seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_summary_guild_date
    ON daily_activity_summary (guild_id, date);
