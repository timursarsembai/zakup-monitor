-- Порядок в каталоге (docs/CATALOG_PLAN.md, этап 6): жалобы на товары и продавцов и скрытие
-- товара сотрудником (blocked — владелец сам не вернёт, в отличие от «снять с продажи»).
ALTER TABLE product_catalog ADD COLUMN IF NOT EXISTS blocked boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS catalog_reports (
    id bigserial PRIMARY KEY,
    product_id bigint REFERENCES product_catalog(id) ON DELETE CASCADE,
    seller_id bigint REFERENCES sellers(id) ON DELETE CASCADE,
    user_id bigint REFERENCES users(id) ON DELETE SET NULL,
    reason text NOT NULL CHECK (reason IN ('wrong', 'fraud', 'forbidden', 'spam', 'other')),
    comment text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    decided_by bigint REFERENCES users(id) ON DELETE SET NULL,
    CHECK (product_id IS NOT NULL OR seller_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS catalog_reports_open ON catalog_reports (created_at) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS catalog_reports_user ON catalog_reports (user_id, created_at);
-- Одна открытая жалоба пользователя на товар или продавца (и при двойном нажатии).
CREATE UNIQUE INDEX IF NOT EXISTS catalog_reports_one_open
    ON catalog_reports (user_id, coalesce(product_id, 0), coalesce(seller_id, 0)) WHERE status = 'open';
