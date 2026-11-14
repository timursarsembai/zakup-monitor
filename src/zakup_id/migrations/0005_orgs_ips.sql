-- Организация клиента (форма, БИН/ИИН, название) — у ИП это ИИН и имя человека, поэтому
-- здесь, а не на основном сайте. Сайт спрашивает её, когда нужно (итоги «это вы», сотрудникам).
CREATE TABLE IF NOT EXISTS orgs (
    account_id bigint PRIMARY KEY,   -- номер пользователя сайта (= номер записи здесь)
    kind       text,                 -- 'ip', 'too', 'other'
    bin        text,
    name       text,
    results_bin text,                -- «БИН для итогов» сотрудника (иначе — bin)
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- IP-адреса журнала действий сайта: сам журнал — там, адрес — здесь, по номеру записи.
CREATE TABLE IF NOT EXISTS action_ips (
    action_id  bigint PRIMARY KEY,
    account_id bigint,
    ip         text NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS action_ips_at ON action_ips (at);
