-- Черновики технических предложений поставщика (zakup.proposals): по лоту и выбранному
-- товару из подбора, общие для всех, как вопросы поставщику.
CREATE TABLE IF NOT EXISTS tech_proposals (
    lot_id      bigint NOT NULL,
    url         text NOT NULL,
    status      text NOT NULL DEFAULT 'queued',
    provider    text NOT NULL,
    model       text,
    result      jsonb,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (lot_id, url)
);
