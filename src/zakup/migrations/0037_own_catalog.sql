-- Свой каталог продавца в кабинете (zakup.shop, docs/CATALOG_PLAN.md, этап 3).
-- Свои товары — в том же product_catalog (одни страницы, поиск и фильтры): own = true,
-- адреса на чужом сайте нет (url пустой), url_key — 'own:<случайный>'. on_sale = false —
-- снят с продажи: не виден в каталоге (и найденный ИИ товар магазина, который владелец снял).
ALTER TABLE product_catalog ALTER COLUMN url DROP NOT NULL;
ALTER TABLE product_catalog
    ADD COLUMN IF NOT EXISTS own boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS on_sale boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS description text,
    ADD COLUMN IF NOT EXISTS enstru text;

-- Цены по количеству: розница — min_qty = 1 (она же product_catalog.price), опт — «от N».
CREATE TABLE IF NOT EXISTS product_prices (
    product_id bigint NOT NULL REFERENCES product_catalog(id) ON DELETE CASCADE,
    min_qty numeric NOT NULL CHECK (min_qty >= 1),
    price numeric NOT NULL CHECK (price > 0),
    PRIMARY KEY (product_id, min_qty)
);

-- «Это мой магазин»: заявка на найденный ИИ магазин, решает сотрудник.
CREATE TABLE IF NOT EXISTS seller_claims (
    id bigserial PRIMARY KEY,
    seller_id bigint NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bin text,
    comment text,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    decided_by bigint REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS seller_claims_open ON seller_claims (seller_id, user_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS sellers_pending ON sellers (created_at) WHERE status = 'pending';
