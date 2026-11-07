-- Idempotent: `zakup migrate` runs this on every start.

-- Объявления (trd-buy). id совпадает с id на портале.
CREATE TABLE IF NOT EXISTS announcements (
    id               bigint PRIMARY KEY,
    number_anno      text,
    name_ru          text,
    name_kz          text,
    total_sum        numeric(20, 2),
    count_lots       integer,
    trade_method_id  integer,
    status_id        integer,
    customer_bin     text,
    customer_name_ru text,
    org_bin          text,
    org_name_ru      text,
    publish_date     timestamptz,
    start_date       timestamptz,
    end_date         timestamptz,
    last_update_date timestamptz,
    raw              jsonb NOT NULL,
    fetched_at       timestamptz NOT NULL DEFAULT now()
);
-- Когда последний раз читали карточку /v3/trd-buy/{id}: только в ней есть заказчик.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS card_fetched_at timestamptz;
-- Подробности из GraphQL (zakup.details): их нет в REST-списке.
-- disable_person — признак «Закупка среди организаций инвалидов» (refTrdBuySigns=1).
-- kato — места поставки: КАТО объявления, лотов и пунктов плана. Первые две
-- цифры — регион: 75 — г. Алматы, 19 — Алматинская область.
-- prepayment_max — наибольший аванс по пунктам плана, в процентах.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS disable_person boolean;
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS kato text[];
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS prepayment_max numeric(6, 2);
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS type_trade_id integer;
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS subject_type_id integer;
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS details_fetched_at timestamptz;
-- last_update_date, при котором читали подробности: изменилось — перечитать.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS details_update_date timestamptz;
-- Полный ответ GraphQL (справочники, организатор, лоты, файлы) для страницы объявления.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS detail jsonb;
CREATE INDEX IF NOT EXISTS announcements_status_end ON announcements (status_id, end_date);
CREATE INDEX IF NOT EXISTS announcements_end_date ON announcements (end_date);
CREATE INDEX IF NOT EXISTS announcements_customer_bin ON announcements (customer_bin);

-- Лоты. Поиск по подпискам идёт по ним: у лота своё название, описание и сумма.
CREATE TABLE IF NOT EXISTS lots (
    id               bigint PRIMARY KEY,
    trd_buy_id       bigint,
    number_anno      text,
    lot_number       text,
    name_ru          text,
    name_kz          text,
    description_ru   text,
    amount           numeric(20, 2),
    count            numeric(20, 3),
    status_id        integer,
    trade_method_id  integer,
    customer_bin     text,
    customer_name_ru text,
    last_update_date timestamptz,
    raw              jsonb NOT NULL,
    fetched_at       timestamptz NOT NULL DEFAULT now(),
    tsv              tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(name_ru, '') || ' ' || coalesce(description_ru, ''))
    ) STORED
);
-- Порядок первого появления лота в базе. Подписки идут по нему, а не по id:
-- id выдаётся черновику, и опубликованный позже лот получает id «из прошлого».
ALTER TABLE lots ADD COLUMN IF NOT EXISTS seen_seq bigint GENERATED ALWAYS AS IDENTITY;
CREATE UNIQUE INDEX IF NOT EXISTS lots_seen_seq ON lots (seen_seq);
CREATE INDEX IF NOT EXISTS lots_tsv ON lots USING gin (tsv);
-- Из пунктов плана (zakup.details): аванс в %, места поставки, цена за единицу,
-- единица измерения, доп. характеристика, срок поставки.
ALTER TABLE lots ADD COLUMN IF NOT EXISTS prepayment numeric(6, 2);
ALTER TABLE lots ADD COLUMN IF NOT EXISTS kato text[];
ALTER TABLE lots ADD COLUMN IF NOT EXISTS price numeric(20, 2);
ALTER TABLE lots ADD COLUMN IF NOT EXISTS unit_name text;
ALTER TABLE lots ADD COLUMN IF NOT EXISTS extra_desc text;
ALTER TABLE lots ADD COLUMN IF NOT EXISTS supply_date text;

-- Текст технической спецификации лота из PDF, для показа на странице.
CREATE TABLE IF NOT EXISTS lot_specs (
    lot_id     bigint PRIMARY KEY,
    file_url   text NOT NULL,
    text       text,
    error      text,
    fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lots_trd_buy_id ON lots (trd_buy_id);
CREATE INDEX IF NOT EXISTS lots_customer_bin ON lots (customer_bin);

-- Состояние синхронизации. API отдаёт записи от новых к старым, search_after=X —
-- это «id меньше X». Всё с id <= last_id уже загружено. Проход сверху вниз
-- начинается с самой новой записи (walk_top) и идёт страницами; resume_after —
-- где он прервался. Когда проход доходит до last_id, last_id := walk_top.
CREATE TABLE IF NOT EXISTS sync_state (
    entity       text PRIMARY KEY,
    last_id      bigint NOT NULL,
    walk_top     bigint,
    resume_after bigint,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS walk_top bigint;
ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS resume_after bigint;

-- Пользователи заложены сразу, чтобы потом открыть сервис другим без переделки схемы.
CREATE TABLE IF NOT EXISTS users (
    id               bigserial PRIMARY KEY,
    email            text UNIQUE NOT NULL,
    telegram_chat_id text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;

-- Сессии входа. В cookie лежит случайный токен, в базе — только его sha256:
-- утечка дампа не даёт войти под пользователем.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash bytea PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    user_agent text
);
CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions (user_id);

-- Попытки входа, для ограничения подбора пароля.
CREATE TABLE IF NOT EXISTS login_attempts (
    id         bigserial PRIMARY KEY,
    email      text NOT NULL,
    ip         text,
    ok         boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_email ON login_attempts (email, created_at);
CREATE INDEX IF NOT EXISTS login_attempts_ip ON login_attempts (ip, created_at);

-- Одноразовые коды для привязки Telegram: сайт даёт ссылку t.me/<бот>?start=<код>,
-- бот по коду узнаёт пользователя. В базе только sha256 кода.
CREATE TABLE IF NOT EXISTS telegram_link_codes (
    code_hash  bytea PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id            bigserial PRIMARY KEY,
    user_id       bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name          text NOT NULL,
    -- Синтаксис websearch_to_tsquery: «ноутбук -аренда», «"бумага а4" or картридж».
    query         text NOT NULL,
    min_amount    numeric(20, 2),
    max_amount    numeric(20, 2),
    customer_bins text[],
    active        boolean NOT NULL DEFAULT true,
    -- Лоты с seen_seq не больше этого уже проверены. Новая подписка начинает с
    -- текущего максимума, чтобы не прислать всю историю разом.
    matched_up_to bigint NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- kind: 'lots' — поиск по словам среди новых лотов (query, min/max_amount);
-- 'announcements' — сохранённый фильтр объявлений (filters, см. zakup.filters).
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'lots';
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS filters jsonb;

-- Какие лоты уже отправлены по подписке: повторно не шлём.
CREATE TABLE IF NOT EXISTS matches (
    subscription_id bigint NOT NULL REFERENCES subscriptions (id) ON DELETE CASCADE,
    lot_id          bigint NOT NULL REFERENCES lots (id) ON DELETE CASCADE,
    matched_at      timestamptz NOT NULL DEFAULT now(),
    notified_at     timestamptz,
    PRIMARY KEY (subscription_id, lot_id)
);

-- 2026-11-07: подписки-фильтры перешли с объявлений на лоты (таблица matches).
-- Лоты уже отправленных объявлений считаем отправленными, чтобы не прислать их снова.
DO $$
BEGIN
    IF to_regclass('announcement_matches') IS NOT NULL THEN
        INSERT INTO matches (subscription_id, lot_id, matched_at, notified_at)
        SELECT am.subscription_id, l.id, am.matched_at, am.notified_at
        FROM announcement_matches am JOIN lots l ON l.trd_buy_id = am.announcement_id
        ON CONFLICT DO NOTHING;
        DROP TABLE announcement_matches;
    END IF;
END $$;

-- Отметки пользователя на лотах: участие ведётся прямо на сайте (zakup.marks).
CREATE TABLE IF NOT EXISTS lot_marks (
    user_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    lot_id     bigint NOT NULL REFERENCES lots (id) ON DELETE CASCADE,
    status     text NOT NULL,
    note       text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Когда поставлен текущий статус: от него считаются напоминания.
    status_at  timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- Самое близкое к окончанию напоминание, которое уже отправлено (часы).
    reminded_hours integer,
    PRIMARY KEY (user_id, lot_id)
);
