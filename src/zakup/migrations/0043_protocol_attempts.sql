-- Протоколы итогов (zakup.results): сетевой сбой раньше повторялся на каждом круге без
-- ограничения — ночной отчёт 2026-11-13: 554 таймаута за сутки, одни и те же файлы. Теперь
-- между попытками пауза: час × число неудачных попыток, не больше суток (results.PROTOCOL_DUE).
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS protocol_attempts smallint NOT NULL DEFAULT 0;
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS protocol_tried_at timestamptz;
