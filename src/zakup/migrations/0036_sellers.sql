-- Продавцы каталога товаров (zakup.sellers, docs/CATALOG_PLAN.md, этап 2).
-- kind: found — магазин, найденный поиском с ИИ (domain + seller_key: на маркетплейсе — продавец,
-- у прочих сайтов — весь сайт, seller_key = ''; как seller_contacts); own — свой магазин
-- пользователя (этап 3, user_id). Поля проверки и модерации — для этапов 3 и 6.
CREATE TABLE IF NOT EXISTS sellers (
    id bigserial PRIMARY KEY,
    kind text NOT NULL DEFAULT 'found' CHECK (kind IN ('found', 'own')),
    domain text,
    seller_key text,
    user_id bigint UNIQUE REFERENCES users(id) ON DELETE SET NULL,
    name text NOT NULL,
    bin text,
    verified boolean NOT NULL DEFAULT false,
    verified_at timestamptz,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending', 'hidden')),
    phones jsonb NOT NULL DEFAULT '[]',
    whatsapp text,
    email text,
    website text,
    city text,
    regions text[] NOT NULL DEFAULT '{}',
    description text,
    prices_with_vat boolean,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (domain, seller_key)
);
CREATE INDEX IF NOT EXISTS sellers_name_trgm ON sellers USING gin (name gin_trgm_ops);
ALTER TABLE product_catalog ADD COLUMN IF NOT EXISTS seller_id bigint REFERENCES sellers(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS product_catalog_seller ON product_catalog (seller_id);
