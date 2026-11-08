-- Источники, которые добавил клиент, попадают в подсказку ИИ для всех только после
-- проверки сотрудником (их текст — в подборе у каждого клиента). Прежние — проверены.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS checked boolean NOT NULL DEFAULT true;
