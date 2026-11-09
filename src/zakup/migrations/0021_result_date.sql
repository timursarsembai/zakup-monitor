-- Дата итогов прямо в итоге лота, с индексом: «Итоги тендеров» (/results) отбирают и
-- сортируют по ней, не соединяя каждый итог с объявлением (было 0,3–0,8 с).
-- Заполняют zakup.results (при разборе протокола) и zakup.details.save (дата из подробностей).
-- По умолчанию — момент записи (для записей, где дату не передали).
ALTER TABLE lot_results ADD COLUMN IF NOT EXISTS result_date timestamptz DEFAULT now();
UPDATE lot_results r SET result_date = coalesce(a.itogi_date, r.parsed_at)
FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
WHERE l.id = r.lot_id;
UPDATE lot_results r SET result_date = r.parsed_at
WHERE NOT EXISTS (SELECT 1 FROM lots l WHERE l.id = r.lot_id);
CREATE INDEX IF NOT EXISTS lot_results_date ON lot_results (result_date DESC);
CREATE INDEX IF NOT EXISTS lot_results_outcome_date ON lot_results (outcome, result_date DESC);
