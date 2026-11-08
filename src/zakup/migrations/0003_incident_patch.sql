-- Дежурный больше не выкладывает сам: правка ждёт одобрения владельца в сбое
-- (статусы proposed → approved | rejected, zakup.autofix).
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS patch text;
