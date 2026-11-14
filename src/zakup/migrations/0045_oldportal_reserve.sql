-- Резерв лотов со страниц old.goszakup.gov.kz (zakup.oldportal), пока список лотов OWS стоит.
-- Номера OWS у таких лотов нет — у них временный номер от db.TEMP_LOT_BASE; когда лот приходит
-- из OWS, временный заменяется настоящим (db.adopt_temp_lot), а ссылка на старый ведёт на новый.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS oldportal_checked_at timestamptz;
CREATE INDEX IF NOT EXISTS lots_temp ON lots (trd_buy_id, lot_number) WHERE id >= 9000000000000;
CREATE TABLE IF NOT EXISTS lot_aliases (
    old_id bigint PRIMARY KEY,
    new_id bigint,  -- NULL — лота в OWS не нашлось: ссылка ведёт на объявление
    announcement_id bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Замена временного лота переносит строки по lot_id — без этих индексов каждая замена
-- читала бы таблицы целиком.
CREATE INDEX IF NOT EXISTS matches_lot ON matches (lot_id);
CREATE INDEX IF NOT EXISTS lot_marks_lot ON lot_marks (lot_id);
CREATE INDEX IF NOT EXISTS chat_lots_lot ON chat_lots (lot_id);
