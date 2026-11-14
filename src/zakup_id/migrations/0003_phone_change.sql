-- Смена телефона: код уходит на новый номер, шаг привязан к записи, которая его начала
-- (чужая сессия не завершит чужую смену, даже зная flow).
ALTER TABLE otp_requests ADD COLUMN IF NOT EXISTS account_id bigint;
