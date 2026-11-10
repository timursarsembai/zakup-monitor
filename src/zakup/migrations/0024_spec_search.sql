-- Поиск слов и в тексте техспецификации (галочка «Искать и в тексте техспецификации»):
-- полнотекстовый индекс по разобранному тексту PDF. Выражение — ровно как в zakup.filters.SPEC_TSV.
CREATE INDEX IF NOT EXISTS lot_specs_text_tsv ON lot_specs USING gin (to_tsvector('russian', coalesce(text, '')));
