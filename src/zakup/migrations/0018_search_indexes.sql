-- Поиск лотов после догрузки истории (300 тыс. лотов): сортировки и фильтр по заказчику
-- без индексов читали всю таблицу — 1–1,4 с. С индексами — первые N строк по порядку.
CREATE INDEX IF NOT EXISTS lots_prepayment ON lots (prepayment DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS lots_amount ON lots (amount DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS announcements_publish ON announcements (publish_date DESC NULLS LAST, id DESC);
-- Название заказчика или организатора — подстрокой (ILIKE '%…%').
CREATE INDEX IF NOT EXISTS announcements_customer_trgm ON announcements USING gin (customer_name_ru gin_trgm_ops);
CREATE INDEX IF NOT EXISTS announcements_org_trgm ON announcements USING gin (org_name_ru gin_trgm_ops);
CREATE INDEX IF NOT EXISTS lots_customer_trgm ON lots USING gin (customer_name_ru gin_trgm_ops);
