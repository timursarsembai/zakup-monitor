-- Справочник организаций /orgs (zakup.orgs): все заказчики, организаторы, победители и
-- участники с суммами, регионом и формой (ИП, ТОО, АО, ГУ…). Пересчитывается сборщиком
-- (zakup subjects, раз в 15 минут): REFRESH MATERIALIZED VIEW CONCURRENTLY.
-- Регион — самый частый регион закупок заказчика, у поставщика — регион поставки его лотов.
CREATE MATERIALIZED VIEW IF NOT EXISTS org_directory AS
WITH buyers AS (
    SELECT x.bin, count(*) AS buys, coalesce(sum(a.total_sum), 0) AS buy_amount,
           count(*) FILTER (WHERE a.end_date > now()) AS open_now,
           max(CASE WHEN x.bin = a.customer_bin THEN a.customer_name_ru ELSE a.org_name_ru END) AS name,
           mode() WITHIN GROUP (ORDER BY left(a.kato[1], 2)) AS region
    FROM announcements a
    CROSS JOIN LATERAL (SELECT DISTINCT b AS bin FROM unnest(ARRAY[a.customer_bin, a.org_bin]) b) x
    WHERE x.bin ~ '^\d{12}$' GROUP BY 1),
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
    SELECT e.bin, coalesce(s.name_ru, b.name, w.name, p.name) AS name_ru, s.name_kz,
           coalesce(b.region, w.region, p.region) AS region,
           coalesce(b.buys, 0) AS buys, coalesce(b.buy_amount, 0) AS buy_amount, coalesce(b.open_now, 0) AS open_now,
           coalesce(w.wins, 0) AS wins, coalesce(w.win_amount, 0) AS win_amount, coalesce(p.offers, 0) AS offers
    FROM everyone e LEFT JOIN buyers b USING (bin) LEFT JOIN sellers w USING (bin)
    LEFT JOIN bidders p USING (bin) LEFT JOIN subjects s ON s.bin = e.bin)
SELECT n.*,
       CASE WHEN substr(n.bin, 5, 1) IN ('0', '1', '2', '3')
                 OR n.name_ru ~* '^\s*(ИП|индивидуальный предприниматель)([^А-Яа-яЁёA-Za-z]|$)' THEN 'ip'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])ТОО([^А-Яа-яЁёA-Za-z]|$)|товариществ[оа] с ограниченной' THEN 'too'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])[ПН]?АО([^А-Яа-яЁёA-Za-z]|$)|акционерн(ое|ого) обществ' THEN 'ao'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])(ОО|ОЮЛ)([^А-Яа-яЁёA-Za-z]|$)|общественн(ое|ого) объединени|объединение юридических' THEN 'oo'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])(ГКП|КГП|РГП|ГККП|КГКП|РГКП|ККГП|МКП|ГМП|ГП)([^А-Яа-яЁёA-Za-z]|$)|предприяти' THEN 'gp'
            WHEN n.name_ru ~* '(^|[^А-Яа-яЁёA-Za-z])(ГУ|КГУ|РГУ|ГККУ)([^А-Яа-яЁёA-Za-z]|$)|учреждени|аппарат|акимат|управлени|отдел' THEN 'gu'
            ELSE 'other' END AS form
FROM named n;
CREATE UNIQUE INDEX IF NOT EXISTS org_directory_bin ON org_directory (bin);
CREATE INDEX IF NOT EXISTS org_directory_buy_amount ON org_directory (buy_amount DESC);
CREATE INDEX IF NOT EXISTS org_directory_win_amount ON org_directory (win_amount DESC);
