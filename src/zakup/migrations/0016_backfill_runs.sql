-- Догрузка истории в прошлое, запуски из настроек (zakup.history): за какой период,
-- статус, счётчики до и после — чтобы видеть, сколько прибавилось.
CREATE TABLE IF NOT EXISTS backfill_runs (
    id bigserial PRIMARY KEY,
    since date NOT NULL,
    -- По какую дату: самое старое, что было в базе до запуска.
    upto date,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'stopped', 'done', 'failed')),
    created_by bigint REFERENCES users (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    -- Граница и верх прохода по каждой сущности: {"announcements": {"floor": .., "top": ..}}.
    bounds jsonb NOT NULL DEFAULT '{}',
    before jsonb,
    after jsonb,
    note text,
    error text
);

-- Первая строка истории — первичная загрузка, с которой база начиналась.
INSERT INTO backfill_runs (since, status, created_at, started_at, finished_at, note)
SELECT (min(publish_date) AT TIME ZONE 'Asia/Almaty')::date, 'done', min(fetched_at),
       min(fetched_at), min(fetched_at), 'Первичная загрузка'
FROM announcements HAVING count(*) > 0;
