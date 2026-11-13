-- Резерв (zakup.reserve._files) дописывает документы объявлениям с платформы, сохранённым без
-- них. Без индекса запрос распаковывал detail у каждого открытого объявления (~10 с на каждом
-- проходе). Таких объявлений — сотни/тысячи, частичный индекс маленький. Условие — то же,
-- что в запросе, буква в букву.
CREATE INDEX IF NOT EXISTS announcements_platform_no_files ON announcements (end_date)
    WHERE detail->>'source' = 'platform' AND NOT detail ? 'platformFiles';
