-- Оценка рынка теперь по плановым ценам таких же лотов (zakup.lot_insights.market): все
-- оценки — в очередь на пересчёт.
UPDATE lot_scores SET computed_at = '2000-01-01';
