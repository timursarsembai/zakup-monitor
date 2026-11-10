-- Раздел (категория) для лотов без кода ЕНС ТРУ — по названию: у половины лотов портал не
-- отдаёт код (пустые «Планы» в API). Таблица «название → раздел» строится по лотам с кодом
-- (zakup.details.refresh_name_divisions): только надёжные — не меньше 3 лотов и 70% в одном
-- разделе. Лоту без кода ставится этот раздел с пометкой divisions_guessed; настоящий код,
-- если придёт, заменит догадку.
CREATE TABLE IF NOT EXISTS name_divisions (
    name text PRIMARY KEY,         -- lower(btrim(название лота))
    division text NOT NULL,
    lots integer NOT NULL,
    share real NOT NULL
);
ALTER TABLE lots ADD COLUMN IF NOT EXISTS divisions_guessed boolean NOT NULL DEFAULT false;
-- Замер задержки лотов (настройки → «Данные»): выборка за сутки — по индексу, а не по всей таблице.
CREATE INDEX IF NOT EXISTS announcements_lots_ready_at ON announcements (lots_ready_at) WHERE lots_ready_at IS NOT NULL;
