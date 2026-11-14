-- Регистрация и восстановление пароля по SMS (та же логика, что zakup.signup — те же таблицы).
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
-- Удалённому разрешили зарегистрироваться заново с тем же номером.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS may_reregister boolean NOT NULL DEFAULT false;
-- Прежняя (удалённая) запись того же номера: сайт переносит с неё пробный срок — один на номер.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS previous_id bigint;
CREATE INDEX IF NOT EXISTS accounts_phone ON accounts (phone) WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS otp_requests (
    id          bigserial PRIMARY KEY,
    flow        text NOT NULL UNIQUE,
    purpose     text NOT NULL,                 -- 'register' или 'reset'
    phone       text NOT NULL,
    request_id  text NOT NULL,
    ip          text,
    attempts    integer NOT NULL DEFAULT 0,
    verified_at timestamptz,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS otp_requests_created ON otp_requests (created_at);

-- Настройки (registration_open — открыта ли регистрация; меняет сайт через /api/registration).
CREATE TABLE IF NOT EXISTS app_settings (
    key   text PRIMARY KEY,
    value text NOT NULL
);
