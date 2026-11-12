-- Номер объявления на единой платформе zakup.gov.kz: старый портал (v3bl/old.goszakup.gov.kz)
-- отключили, ссылка теперь https://zakup.gov.kz/announcement/<platform_id> — и номер там свой,
-- не OWS. Заполняют резерв (zakup.reserve: список платформы, поиск по номеру) и переход
-- /a/<id>/portal; у Самрук-Казыны — сборщик skk.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS platform_id bigint;
-- Когда искали на платформе и не нашли: снова — не раньше чем через сутки (резерв и переход).
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS platform_checked_at timestamptz;
UPDATE announcements SET platform_id = (raw->>'platform_id')::bigint
 WHERE system = 3 AND platform_id IS NULL AND raw ? 'platform_id';
UPDATE announcements SET platform_id = (detail->>'platformId')::bigint
 WHERE system = 1 AND platform_id IS NULL AND publish_date > now() - interval '40 days' AND detail ? 'platformId';
-- Добор: открытые без номера — по сроку окончания.
CREATE INDEX IF NOT EXISTS announcements_no_platform_id ON announcements (end_date)
 WHERE platform_id IS NULL AND system = 1;
