-- Объяснение рекомендуемой цены заявки от ИИ (zakup.bids): одно на лот, общее для всех;
-- цифры считаются заново при каждом открытии, текст живёт неделю.
CREATE TABLE IF NOT EXISTS bid_advice (
    lot_id      bigint PRIMARY KEY,
    status      text NOT NULL DEFAULT 'queued',
    provider    text NOT NULL,
    model       text,
    result      jsonb,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz
);
