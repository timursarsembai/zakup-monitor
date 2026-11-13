-- Добор лотов с платформы (reserve._backlog): открытые объявления без лотов, новые первыми.
CREATE INDEX IF NOT EXISTS announcements_without_lots ON announcements (publish_date)
    WHERE NOT lots_ready AND system = 1;
