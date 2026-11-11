-- Служба archive ищет завершённые объявления без полных подробностей (details.refresh_finished).
-- Без индекса запрос шёл по всей таблице и распаковывал detail у каждого завершённого
-- объявления — 40+ с на каждом круге, базе это стоило целое ядро. Таких объявлений — сотни,
-- частичный индекс по ним крошечный. Условие — то же, что в запросе, буква в букву.
CREATE INDEX IF NOT EXISTS announcements_finished_incomplete ON announcements (end_date DESC)
    WHERE status_id IN (350, 410, 430)
      AND (detail IS NULL OR NOT detail ? 'nameKz'
           OR (detail->>'refBuyStatusId')::int IS DISTINCT FROM status_id);
