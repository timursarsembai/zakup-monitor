-- Справочник: заказчики и по лотам (в совместных закупках у лота свой заказчик), имя
-- заказчика — из лота или объявления (org_name_ru портал не заполняет).
DROP MATERIALIZED VIEW IF EXISTS org_directory;
CREATE MATERIALIZED VIEW org_directory AS
WITH roles AS (
    -- Заказчик лота (в совместных закупках у лота свой заказчик) и заказчик/организатор
    -- объявления; одна строка на организацию и объявление.
    SELECT l.customer_bin AS bin, l.trd_buy_id AS aid, sum(l.amount) AS amount, max(l.customer_name_ru) AS name
    FROM lots l WHERE l.customer_bin ~ '^\d{12}$' AND l.trd_buy_id IS NOT NULL GROUP BY 1, 2
    UNION ALL
    SELECT x.bin, a.id, a.total_sum, CASE WHEN x.bin = a.customer_bin THEN a.customer_name_ru END
    FROM announcements a
    CROSS JOIN LATERAL (SELECT DISTINCT b AS bin FROM unnest(ARRAY[a.customer_bin, a.org_bin]) b) x
    WHERE x.bin ~ '^\d{12}$'),
per_announcement AS (
    SELECT bin, aid, max(amount) AS amount, max(name) AS name FROM roles GROUP BY 1, 2),
buyers AS (
    SELECT p.bin, count(*) AS buys, coalesce(sum(p.amount), 0) AS buy_amount,
           count(*) FILTER (WHERE a.end_date > now()) AS open_now, max(p.name) AS name,
           mode() WITHIN GROUP (ORDER BY left(a.kato[1], 2)) AS region
    FROM per_announcement p JOIN announcements a ON a.id = p.aid GROUP BY 1),
sellers AS (
    SELECT r.winner_bin AS bin, count(*) AS wins, coalesce(sum(r.winner_total), 0) AS win_amount,
           max(r.winner_name) AS name,
           mode() WITHIN GROUP (ORDER BY left((coalesce(nullif(l.kato, '{}'), a.kato))[1], 2)) AS region
    FROM lot_results r JOIN lots l ON l.id = r.lot_id LEFT JOIN announcements a ON a.id = l.trd_buy_id
    WHERE r.outcome = 'winner' AND r.winner_bin ~ '^\d{12}$' GROUP BY 1),
bidders AS (
    SELECT o->>'bin' AS bin, count(*) AS offers, max(o->>'name') AS name,
           mode() WITHIN GROUP (ORDER BY left((coalesce(nullif(l.kato, '{}'), a.kato))[1], 2)) AS region
    FROM lot_results r CROSS JOIN LATERAL jsonb_array_elements(r.offers) o
    JOIN lots l ON l.id = r.lot_id LEFT JOIN announcements a ON a.id = l.trd_buy_id
    WHERE o->>'bin' ~ '^\d{12}$' GROUP BY 1),
everyone AS (SELECT bin FROM buyers UNION SELECT bin FROM sellers UNION SELECT bin FROM bidders),
named AS (
    SELECT e.bin, coalesce(s.name_ru, b.name, w.name, p.name) AS name_ru, s.name_kz, s.kopf,
           coalesce(CASE WHEN left(s.kato[1], 2) ~ '^\d\d$' THEN left(s.kato[1], 2) END, b.region, w.region, p.region) AS region,
           coalesce(b.buys, 0) AS buys, coalesce(b.buy_amount, 0) AS buy_amount, coalesce(b.open_now, 0) AS open_now,
           coalesce(w.wins, 0) AS wins, coalesce(w.win_amount, 0) AS win_amount, coalesce(p.offers, 0) AS offers
    FROM everyone e LEFT JOIN buyers b USING (bin) LEFT JOIN sellers w USING (bin)
    LEFT JOIN bidders p USING (bin) LEFT JOIN subjects s ON s.bin = e.bin)
SELECT n.*,
       CASE WHEN substr(n.bin, 5, 1) IN ('0', '1', '2', '3') OR n.kopf ~* '^(ИП|КХ|ФХ)' THEN 'ip'
            WHEN n.kopf ~* '^ТОО' THEN 'too'
            WHEN n.kopf ~* '^[ПН]?АО' THEN 'ao'
            WHEN n.kopf ~* '^(ОО|ОЮЛ|ОФ|РОО)' THEN 'oo'
            WHEN n.kopf ~* '^(ГКП|КГП|РГП|ГККП|КГКП|РГКП|ККГП|МКП|ГМП|ГП)' THEN 'gp'
            WHEN n.kopf ~* '^(ГУ|КГУ|РГУ|ГККУ)' THEN 'gu'
            WHEN n.name_ru ~* '^\s*(ИП|индивидуальный предприниматель)([^А-Яа-яЁёA-Za-z]|$)' THEN 'ip'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])ТОО([^А-Яа-яЁёA-Za-z]|$)|товариществ[оа] с ограниченной' THEN 'too'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])[ПН]?АО([^А-Яа-яЁёA-Za-z]|$)|акционерн(ое|ого) обществ' THEN 'ao'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])(ОО|ОЮЛ)([^А-Яа-яЁёA-Za-z]|$)|общественн(ое|ого) объединени|объединение юридических' THEN 'oo'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])(ГКП|КГП|РГП|ГККП|КГКП|РГКП|ККГП|МКП|ГМП|ГП)([^А-Яа-яЁёA-Za-z]|$)|предприяти' THEN 'gp'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])(ГУ|КГУ|РГУ|ГККУ)([^А-Яа-яЁёA-Za-z]|$)|учреждени|аппарат|акимат|управлени|отдел' THEN 'gu'
            ELSE 'other' END AS form
FROM named n;
CREATE UNIQUE INDEX IF NOT EXISTS org_directory_bin ON org_directory (bin);
-- Сортировки справочника: по сумме (как в orgs._ORDER) и по названию; поиск по названию.
CREATE INDEX IF NOT EXISTS org_directory_amount ON org_directory ((greatest(buy_amount, win_amount)) DESC, bin);
CREATE INDEX IF NOT EXISTS org_directory_name ON org_directory (name_ru, bin);
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS org_directory_name_trgm ON org_directory USING gin (name_ru gin_trgm_ops, name_kz gin_trgm_ops);

-- Взвешенное среднее снижение цены (было среднее средних по регионам).
DROP MATERIALIZED VIEW IF EXISTS supplier_activity;
CREATE MATERIALIZED VIEW IF NOT EXISTS supplier_activity AS
SELECT o.bin,
       coalesce(l.enstru[1], '') AS code,
       coalesce(left((coalesce(nullif(l.kato, '{}'), a.kato))[1], 2), '') AS region,
       count(*) AS offers,
       count(*) FILTER (WHERE r.outcome = 'winner' AND r.winner_bin = o.bin) AS wins,
       -- Снижение цены: сумма и число побед с ним — среднее считается по всем строкам сразу.
       sum(1 - r.winner_total / l.amount) FILTER (
           WHERE r.outcome = 'winner' AND r.winner_bin = o.bin AND l.amount > 0
             AND r.winner_total BETWEEN 0 AND l.amount) AS discount_sum,
       count(*) FILTER (
           WHERE r.outcome = 'winner' AND r.winner_bin = o.bin AND l.amount > 0
             AND r.winner_total BETWEEN 0 AND l.amount) AS discount_n,
       max(a.end_date) AS last_at
FROM lot_results r
CROSS JOIN LATERAL (
    SELECT e->>'bin' AS bin FROM jsonb_array_elements(coalesce(r.offers, '[]')) e
    UNION SELECT r.winner_bin WHERE r.winner_bin IS NOT NULL) o
JOIN lots l ON l.id = r.lot_id
LEFT JOIN announcements a ON a.id = l.trd_buy_id
WHERE o.bin ~ '^\d{12}$'
GROUP BY 1, 2, 3;
CREATE UNIQUE INDEX IF NOT EXISTS supplier_activity_key ON supplier_activity (bin, code, region);
CREATE INDEX IF NOT EXISTS supplier_activity_code ON supplier_activity (code text_pattern_ops);

-- Подборки по категориям (zakup.catalog): лоты раздела КПВЭД по первому коду ЕНС ТРУ.
CREATE INDEX IF NOT EXISTS lots_division ON lots ((left(enstru[1], 2)));
