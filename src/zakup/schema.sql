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

-- Какие лоты уже отправлены по подписке: повторно не шлём.
CREATE TABLE IF NOT EXISTS matches (
    subscription_id bigint NOT NULL REFERENCES subscriptions (id) ON DELETE CASCADE,
    lot_id          bigint NOT NULL REFERENCES lots (id) ON DELETE CASCADE,
    matched_at      timestamptz NOT NULL DEFAULT now(),
    notified_at     timestamptz,
    PRIMARY KEY (subscription_id, lot_id)
);
