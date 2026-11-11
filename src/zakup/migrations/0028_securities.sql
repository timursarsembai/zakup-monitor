-- Обеспечение заявки и исполнения договора (zakup.securities): проценты из документов
-- объявления (проект договора, конкурсная документация). NULL — ещё не разбирали.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS securities jsonb;
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS securities_at timestamptz;
