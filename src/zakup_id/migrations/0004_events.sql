-- События для основного сайта: Telegram — в Германии (решение владельца 2026-11-14),
-- поэтому, например, предупреждение «телефон изменён» отправляет сайт. Сайт забирает
-- недоставленные (GET /api/events) и подтверждает полученные (POST /api/events/ack):
-- по порядку id нельзя — id выдаётся при вставке, а виден после фиксации, и событие из
-- более долгой транзакции проскочило бы. Без телефонов: только последние 4 цифры.
CREATE TABLE IF NOT EXISTS events (
    id           bigserial PRIMARY KEY,
    account_id   bigint REFERENCES accounts (id) ON DELETE CASCADE,  -- NULL — владельцу сервиса
    kind         text NOT NULL,             -- 'phone_changed', 'owner_alert'
    detail       text,                      -- новый номер '…1122' или текст для владельца
    before       text,                      -- прежний номер '…2233'
    created_at   timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);
CREATE INDEX IF NOT EXISTS events_pending ON events (id) WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS events_delivered ON events (delivered_at) WHERE delivered_at IS NOT NULL;
