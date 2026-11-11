-- Оценки лота (zakup.scores): цифры со страницы лота, посчитанные заранее, — для фильтров
-- поиска. Пишет служба scores: открытые лоты, новые — сразу, остальные — раз в сутки; строки
-- закрытых лотов остаются (последние посчитанные значения).
CREATE TABLE IF NOT EXISTS lot_scores (
    lot_id                bigint PRIMARY KEY REFERENCES lots (id) ON DELETE CASCADE,
    -- Плановая цена за единицу против медианы цен победителей за единицу (+0.2 — на 20% выше).
    market_diff           real,
    market_median         numeric,
    market_n              integer,
    -- Тот же лот у того же заказчика раньше: сколько раз и сколько не состоялось.
    repeats               integer NOT NULL DEFAULT 0,
    repeats_failed        integer NOT NULL DEFAULT 0,
    -- «Свой поставщик»: такой товар у заказчика почти всегда выигрывает он.
    same_winner_bin       text,
    same_winner_name      text,
    -- Доля несостоявшихся закупок заказчика (все его итоги).
    customer_failed_share real,
    customer_results      integer,
    -- Деньги на заморозку (₸): обеспечение заявки, договора с авансом (от плановой суммы), всего.
    bid_deposit           numeric,
    contract_deposit      numeric,
    freeze_total          numeric,
    dumping_floor         numeric,
    computed_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lot_scores_market ON lot_scores (market_diff) WHERE market_diff IS NOT NULL;
CREATE INDEX IF NOT EXISTS lot_scores_freeze ON lot_scores (freeze_total);
CREATE INDEX IF NOT EXISTS lot_scores_same_winner ON lot_scores (lot_id) WHERE same_winner_bin IS NOT NULL;
CREATE INDEX IF NOT EXISTS lot_scores_computed ON lot_scores (computed_at);
