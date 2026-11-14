-- Учётные записи. id — тот же номер пользователя, что на основном сайте (при переносе
-- старые номера сохраняются, новые выдаёт счётчик — после переноса accounts.sync_ids).
CREATE TABLE IF NOT EXISTS accounts (
    id               bigserial PRIMARY KEY,
    login            text UNIQUE NOT NULL,          -- телефон +7XXXXXXXXXX или e-mail сотрудника
    password_hash    text,
    phone            text,
    email            text,
    disabled         boolean NOT NULL DEFAULT false,
    -- Растёт при «выйти везде»: старые метки «этот браузер уже входил» больше не действуют.
    device_gen       integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Сессии входа: в жетоне — случайный sid, здесь — только его sha256.
CREATE TABLE IF NOT EXISTS sessions (
    sid_hash   bytea PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    -- Поколение жетона: при продлении растёт, прежние жетоны этой сессии перестают продлеваться.
    gen        integer NOT NULL DEFAULT 0,
    renewed_at timestamptz,
    user_agent text,
    ip         text
);
CREATE INDEX IF NOT EXISTS sessions_account ON sessions (account_id);
CREATE INDEX IF NOT EXISTS sessions_expires ON sessions (expires_at);

-- Попытки входа — ограничение подбора пароля; старше 30 дней удаляются.
CREATE TABLE IF NOT EXISTS login_attempts (
    id         bigserial PRIMARY KEY,
    login      text NOT NULL,
    ip         text,
    ok         boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_login ON login_attempts (login, created_at);
CREATE INDEX IF NOT EXISTS login_attempts_ip ON login_attempts (ip, created_at);
CREATE INDEX IF NOT EXISTS login_attempts_created ON login_attempts (created_at);
