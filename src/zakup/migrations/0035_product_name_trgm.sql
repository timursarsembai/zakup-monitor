-- «Похожие товары» на открытой странице товара (zakup.products.get): поиск по близости
-- названия (pg_trgm, оператор %) — по индексу, а не перебором всего каталога.
CREATE INDEX IF NOT EXISTS product_catalog_name_trgm ON product_catalog USING gin (name gin_trgm_ops);
