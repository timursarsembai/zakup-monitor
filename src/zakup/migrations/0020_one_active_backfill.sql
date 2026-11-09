-- Не больше одного идущего или ждущего запуска догрузки: две копии сайта или двойное
-- нажатие «Запустить» не создают второй запуск.
CREATE UNIQUE INDEX IF NOT EXISTS backfill_runs_one_active ON backfill_runs ((true))
    WHERE status IN ('queued', 'running');
