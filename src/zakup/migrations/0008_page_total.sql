-- Первая выборка подписки — не больше matcher.PAGE_LOTS_LIMIT лотов (ближайшие по сроку);
-- total — сколько подходило всего, чтобы сказать, что показана часть.
ALTER TABLE match_pages ADD COLUMN IF NOT EXISTS total integer;
