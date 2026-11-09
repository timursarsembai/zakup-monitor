-- Замер задержки лотов (обсуждение сбора через казахстанский сервер): когда объявление
-- впервые попало в базу и когда в его подробностях пришли лоты. У старых записей
-- first_seen_at — время миграции, считаем только новые.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS first_seen_at timestamptz DEFAULT now();
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS lots_ready_at timestamptz;
