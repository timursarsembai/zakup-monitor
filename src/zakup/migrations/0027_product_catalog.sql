-- Каталог товаров из поиска с ИИ (zakup.products): каждое найденное предложение — один раз
-- по ссылке. Новый поиск сначала берёт подходящее отсюда (свежую цену — без открытия
-- страницы), в интернете ищет только недостающее. Страница /products — сотрудникам.
CREATE TABLE IF NOT EXISTS product_catalog (
    id bigserial PRIMARY KEY,
    url_key text NOT NULL UNIQUE,
    url text NOT NULL,
    name text NOT NULL,
    seller text,
    city text,
    phone text,
    image text,
    price numeric,
    price_unit text,
    -- характеристики самого товара со страницы продавца: [{"name": ..., "value": ...}]
    specs jsonb NOT NULL DEFAULT '[]',
    division text,
    lots bigint[] NOT NULL DEFAULT '{}',
    available boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    -- когда цену видели на странице: «цена на дату»
    checked_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- прежние цены: [{"at": ..., "price": ...}]
    price_history jsonb NOT NULL DEFAULT '[]',
    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(name, '') || ' ' || coalesce(seller, ''))) STORED
);
CREATE INDEX IF NOT EXISTS product_catalog_tsv ON product_catalog USING gin (tsv);
CREATE INDEX IF NOT EXISTS product_catalog_division ON product_catalog (division);
CREATE INDEX IF NOT EXISTS product_catalog_checked ON product_catalog (checked_at DESC);
