"""Прикладные цифры на странице лота — чтобы решить, стоит ли участвовать.

«Такой же товар» — тот же код ЕНС ТРУ, а у лотов без кода (портал не отдаёт его у половины)
— те же слова в названии, как в блоке «Конкуренты и поставщики» (zakup.suppliers).

- history: что этот заказчик покупал из такого же раньше — цена, кто выиграл, сколько заявок;
  если почти всегда выигрывает один поставщик — предупреждение;
- repeats: этот же лот (то же название, сумма ±20%) у заказчика уже объявляли — чем кончилось;
- market: плановая цена за единицу против цены победителей за единицу (та же единица измерения);
- customer: как проходят закупки заказчика — доля несостоявшихся и повторно объявленных;
- similar_where: открытые лоты с таким же товаром у других заказчиков (вкладка «Похожие
  открытые лоты»).
"""

from datetime import datetime
from typing import Any

import psycopg

from zakup.catalog import open_filter
from zakup.suppliers import same_product

PERIOD = "365 days"
HISTORY_ROWS = 10
MARKET_MIN = 5  # меньше итогов — «цену рынка» не показываем
SAME_WINNER_MIN = 3  # предупреждение «свой поставщик»: побед одного не меньше
SAME_WINNER_SHARE = 0.7  # …и не меньше этой доли итогов с победителем
SIMILAR_LIMIT = 60


def lot_facts(conn: psycopg.Connection, lot_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT l.trd_buy_id, l.customer_bin, coalesce(l.enstru[1], ''),
                  coalesce(l.name_ru, ''), coalesce(lower(l.unit_name), ''), l.amount, l.count, a.publish_date,
                  a.securities, a.trade_method_id, l.prepayment
           FROM lots l JOIN announcements a ON a.id = l.trd_buy_id WHERE l.id = %s""", (lot_id,)).fetchone()
    if not row:
        return None
    keys = ("announcement", "customer", "code", "name", "unit", "amount", "count", "published",
            "securities", "method", "prepayment")
    return dict(zip(keys, row, strict=True))


def _match(facts: dict[str, Any]) -> str:
    """Условие «такой же товар» для лотов l (параметры code, name) — как у конкурентов."""
    return same_product("code" if len(facts["code"]) >= 2 else "name")


def _rows(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def history(conn: psycopg.Connection, lot_id: int, facts: dict[str, Any]) -> dict[str, Any] | None:
    if not facts["customer"] or not (facts["code"] or facts["name"].strip()):
        return None
    params = {**facts, "lot": lot_id}
    source = f"""FROM lots l JOIN lot_results r ON r.lot_id = l.id
                 WHERE l.customer_bin = %(customer)s AND l.id <> %(lot)s AND {_match(facts)}"""
    rows = _rows(conn.execute(
        f"""SELECT l.id, l.name_ru, l.name_kz, l.amount, l.count, l.unit_name, l.unit_name_kz, a.end_date,
                   r.outcome, r.winner_bin, r.winner_name, r.winner_total, jsonb_array_length(r.offers) AS offers
            FROM lots l JOIN announcements a ON a.id = l.trd_buy_id JOIN lot_results r ON r.lot_id = l.id
            WHERE l.customer_bin = %(customer)s AND l.id <> %(lot)s AND {_match(facts)}
            ORDER BY a.end_date DESC NULLS LAST LIMIT {HISTORY_ROWS}""", params))
    if not rows:
        return None
    # Итоги и «свой поставщик» — по всем его закупкам такого товара, не только по показанным.
    total, won = conn.execute(
        f"SELECT count(*), count(*) FILTER (WHERE r.outcome = 'winner') {source}", params).fetchone()
    top = conn.execute(
        f"""SELECT r.winner_bin, max(r.winner_name), count(*) {source} AND r.outcome = 'winner'
            AND r.winner_bin IS NOT NULL GROUP BY 1 ORDER BY 3 DESC LIMIT 1""", params).fetchone()
    same = None
    if top and won and top[2] >= SAME_WINNER_MIN and top[2] / won >= SAME_WINNER_SHARE:
        same = {"bin": top[0], "name": top[1], "wins": top[2], "of": won}
    return {"rows": rows, "total": total, "won": won, "same_winner": same}


def repeats(conn: psycopg.Connection, lot_id: int, facts: dict[str, Any]) -> dict[str, Any] | None:
    """Тот же лот у того же заказчика раньше: то же название и сумма в пределах ±20%."""
    if not facts["customer"] or not facts["name"].strip() or not facts["amount"]:
        return None
    rows = _rows(conn.execute(
        """SELECT l.id, a.publish_date, a.end_date, l.amount, r.outcome, r.winner_name,
                  jsonb_array_length(r.offers) AS offers
           FROM lots l JOIN announcements a ON a.id = l.trd_buy_id LEFT JOIN lot_results r ON r.lot_id = l.id
           WHERE l.customer_bin = %(customer)s AND l.id <> %(lot)s AND l.trd_buy_id <> %(announcement)s
             AND lower(btrim(l.name_ru)) = lower(btrim(%(name)s))
             AND l.amount BETWEEN %(amount)s * 0.8 AND %(amount)s * 1.2
             AND a.publish_date < %(published)s
           ORDER BY a.publish_date DESC LIMIT 10""", {**facts, "lot": lot_id}))
    if not rows:
        return None
    return {"rows": rows, "failed": sum(1 for r in rows if r["outcome"] == "failed")}


def market(conn: psycopg.Connection, lot_id: int, facts: dict[str, Any]) -> dict[str, Any] | None:
    """Цена за единицу у победителей такого же товара за год — в той же единице измерения."""
    if not facts["amount"] or not facts["count"]:
        return None
    row = conn.execute(
        f"""SELECT count(*), percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY r.winner_total / l.count)
            FROM lot_results r JOIN lots l ON l.id = r.lot_id
            WHERE r.outcome = 'winner' AND r.winner_total > 0 AND l.count > 0 AND l.id <> %(lot)s
              AND r.result_date > now() - interval '{PERIOD}'
              AND coalesce(lower(l.unit_name), '') = %(unit)s AND {_match(facts)}
              AND r.winner_total BETWEEN l.amount * 0.05 AND l.amount""",
        {**facts, "lot": lot_id}).fetchone()
    n, quartiles = row
    if n < MARKET_MIN or not quartiles:
        return None
    plan = float(facts["amount"]) / float(facts["count"])
    low, median, high = (float(q) for q in quartiles)
    return {"n": n, "plan": plan, "low": low, "median": median, "high": high,
            "diff": plan / median - 1 if median else None}


def customer(conn: psycopg.Connection, facts: dict[str, Any]) -> dict[str, Any] | None:
    """Как проходят закупки заказчика: итоги его лотов и сколько названий он объявлял повторно."""
    if not facts["customer"]:
        return None
    total, failed, empty = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE r.outcome = 'failed'),
                  count(*) FILTER (WHERE r.outcome = 'failed' AND r.offers = '[]')
           FROM lot_results r JOIN lots l ON l.id = r.lot_id WHERE l.customer_bin = %(customer)s""",
        facts).fetchone()
    names, repeated = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE n > 1)
           FROM (SELECT count(DISTINCT trd_buy_id) AS n FROM lots WHERE customer_bin = %(customer)s
                       AND name_ru IS NOT NULL AND btrim(name_ru) <> ''
                 GROUP BY lower(btrim(name_ru))) x""", facts).fetchone()
    if not total and not names:
        return None
    return {"results": total, "failed": failed, "empty": empty,
            "failed_share": failed / total if total else None,
            "names": names, "repeated": repeated, "repeated_share": repeated / names if names else None}


def similar_where(facts: dict[str, Any], now: datetime) -> tuple[str, dict[str, Any]]:
    """Открытые лоты с таким же товаром в других объявлениях: условие для lots l, announcements a."""
    where, params = open_filter().where(now, "lots")
    return (f"""{where} AND {_match(facts)} AND l.trd_buy_id <> %(announcement)s
                AND l.customer_bin IS DISTINCT FROM %(customer)s""",
            {**params, "code": facts["code"], "name": facts["name"], "announcement": facts["announcement"],
             "customer": facts["customer"]})
