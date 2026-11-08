-- Язык уведомлений в Telegram: ru | kk | both. У пользователя NULL — как язык сайта
-- (users.ui_lang — последний выбранный на сайте), у группы — оба языка.
ALTER TABLE users ADD COLUMN IF NOT EXISTS ui_lang text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_lang text;
ALTER TABLE telegram_groups ADD COLUMN IF NOT EXISTS notify_lang text NOT NULL DEFAULT 'both';
-- Первая выборка новой или изменённой подписки: одно сообщение с листанием по страницам
-- вместо пачки сообщений (zakup.matcher.send_pages / turn_page).
CREATE TABLE IF NOT EXISTS match_pages (
    id              bigserial PRIMARY KEY,
    subscription_id bigint NOT NULL REFERENCES subscriptions (id) ON DELETE CASCADE,
    lot_ids         bigint[] NOT NULL,
    chat_id         text,
    message_id      bigint,
    sent_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS match_pages_unsent ON match_pages (id) WHERE sent_at IS NULL;
