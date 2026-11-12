-- Закупки не только госзакупок: Самрук-Казына (и позже Mitwork) с единой платформы zakup.gov.kz.
-- system: 1 — госзакупки (OWS), 3 — Самрук-Казына (номер системы — как у платформы).
-- Их строки — в тех же таблицах: поиск, подписки, избранное работают одинаково. Номера
-- у Самрук-Казыны — номер платформы + 10^12 (zakup.skk.ID_OFFSET): с OWS не пересекаются.
-- Службы, которые ходят в OWS по номеру объявления/лота, берут только system = 1.
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS system smallint NOT NULL DEFAULT 1;
ALTER TABLE lots ADD COLUMN IF NOT EXISTS system smallint NOT NULL DEFAULT 1;
-- Строк не госзакупок мало: частичные индексы крошечные, госзакупкам не мешают.
CREATE INDEX IF NOT EXISTS announcements_other_systems ON announcements (system, end_date) WHERE system <> 1;
CREATE INDEX IF NOT EXISTS lots_other_systems ON lots (system, trd_buy_id) WHERE system <> 1;

-- Юридические лица из открытого реестра data.egov.kz (набор gbd_ul): названия заказчиков,
-- которых нет ни на платформе, ни в OWS; дальше — основа справочника организаций.
CREATE TABLE IF NOT EXISTS legal_entities (
    bin text PRIMARY KEY,
    name_ru text,
    name_kz text,
    address_ru text,
    director text,
    oked_ru text,
    status_ru text,
    registered text,
    fetched_at timestamptz NOT NULL DEFAULT now()
);
