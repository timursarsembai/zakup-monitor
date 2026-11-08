-- Напоминания по избранному — свой график у каждого (zakup.marks): wd2 — за 2 рабочих
-- дня, 24 — за сутки, 3 — за 3 часа. NULL — по умолчанию (за сутки и за 3 часа).
ALTER TABLE users ADD COLUMN IF NOT EXISTS remind_at text[];
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_results boolean NOT NULL DEFAULT true;
