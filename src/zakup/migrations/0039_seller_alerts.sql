-- Оповещения продавцам «появился лот на ваш товар» (zakup.offers.alerts, этап 5 каталога).
-- sellers.alerts — продавец может выключить; seller_alerts — уже отправленное (без повторов);
-- job_marks — докуда дошли по лотам (lots.seen_seq).
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS alerts boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS seller_alerts (
    seller_id bigint NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    lot_id bigint NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (seller_id, lot_id)
);
CREATE TABLE IF NOT EXISTS job_marks (
    name text PRIMARY KEY,
    value bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
