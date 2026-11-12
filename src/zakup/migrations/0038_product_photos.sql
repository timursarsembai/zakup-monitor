-- Фото своих товаров продавцов (zakup.photos, docs/CATALOG_PLAN.md, этап 4). Файлы — не в базе,
-- а на диске (том media, раздаёт шлюз по /media/): <key>.webp (до 1600 px) и <key>_t.webp
-- (превью 400 px). Первое по position — главное: его превью и в product_catalog.image.
CREATE TABLE IF NOT EXISTS product_photos (
    id bigserial PRIMARY KEY,
    product_id bigint NOT NULL REFERENCES product_catalog(id) ON DELETE CASCADE,
    key text NOT NULL UNIQUE,
    width int NOT NULL,
    height int NOT NULL,
    position int NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS product_photos_product ON product_photos (product_id, position, id);
