-- Готовые списки кодов для фильтров «Регион» и «Категория»: первые 2 знака КАТО (область)
-- и ЕНС ТРУ (раздел КПВЭД). Раньше фильтр разворачивал массивы каждого лота — читал всю
-- таблицу (1–1,5 с); с GIN-индексом по списку — только подходящие строки.
-- Лот: место поставки лота, если нет — объявления. Объявление: его КАТО и разделы его лотов.
-- Заполняет zakup.details.save; старые записи — `zakup codes` (пачками).
CREATE OR REPLACE FUNCTION codes2(text[]) RETURNS text[]
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT coalesce(array_agg(DISTINCT left(x, 2) ORDER BY left(x, 2)), '{}')
          FROM unnest($1) x WHERE length(x) >= 2 $$;
ALTER TABLE lots ADD COLUMN IF NOT EXISTS regions text[];
ALTER TABLE lots ADD COLUMN IF NOT EXISTS divisions text[];
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS regions text[];
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS divisions text[];
CREATE INDEX IF NOT EXISTS lots_regions ON lots USING gin (regions);
CREATE INDEX IF NOT EXISTS lots_divisions ON lots USING gin (divisions);
CREATE INDEX IF NOT EXISTS announcements_regions ON announcements USING gin (regions);
CREATE INDEX IF NOT EXISTS announcements_divisions ON announcements USING gin (divisions);
