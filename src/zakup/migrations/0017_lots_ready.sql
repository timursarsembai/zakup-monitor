-- «Лоты получены»: в подробностях объявления (из них строится страница) есть лоты.
-- Открытое объявление без этой отметки не показывается в поиске и не уходит в рассылку:
-- API портала отдаёт лоты с задержкой. Отметку ставит zakup.details.save.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS lots_ready boolean NOT NULL DEFAULT false;
-- Правило касается только открытых — их и размечаем (несколько тысяч строк).
UPDATE announcements SET lots_ready = true
WHERE end_date > now() AND jsonb_typeof(detail->'Lots') = 'array' AND jsonb_array_length(detail->'Lots') > 0;
