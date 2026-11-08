"""Открытые страницы организаций /org/<БИН>: заказчик — его закупки и кто у него выигрывает,
поставщик — где участвовал и побеждал. Всё из своей базы (объявления, лоты, итоги,
карточки subjects), портал не трогаем.

Страницы — у всех участников с БИН или ИИН (12 цифр), и у ИП тоже: это открытые данные
портала (решение владельца 2026-11-09).
"""

import re
from datetime import UTC, datetime
from typing import Any

import psycopg

from zakup import subjects

_BIN = re.compile(r"^\d{12}$")
LIST_LIMIT = 30


def has_page(bin_: str | None) -> bool:
    """БИН или ИИН — 12 цифр."""
    return bool(bin_ and _BIN.match(bin_))


def _rows(conn: psycopg.Connection, sql: str, params: tuple | dict) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _name(conn: psycopg.Connection, bin_: str) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT name_ru, name_kz FROM subjects WHERE bin = %s AND name_ru IS NOT NULL",
                       (bin_,)).fetchone()
    if row:
        return row[0], row[1]
    row = conn.execute(
        """SELECT CASE WHEN customer_bin = %(b)s THEN customer_name_ru ELSE org_name_ru END
           FROM announcements WHERE customer_bin = %(b)s OR org_bin = %(b)s LIMIT 1""",
        {"b": bin_}).fetchone() or conn.execute(
        "SELECT winner_name FROM lot_results WHERE winner_bin = %s LIMIT 1", (bin_,)).fetchone() or conn.execute(
        # Только подавал заявки — имя из списка заявок.
        """SELECT o->>'name' FROM lot_results r, jsonb_array_elements(r.offers) o
           WHERE r.offers @> jsonb_build_array(jsonb_build_object('bin', %(b)s::text)) AND o->>'bin' = %(b)s
           LIMIT 1""", {"b": bin_}).fetchone()
    return (row[0], None) if row else (None, None)


def profile(conn: psycopg.Connection, bin_: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Всё для страницы организации; None — такой организации у нас нет."""
    if not has_page(bin_):
        return None
    now = now or datetime.now(UTC)
    name_ru, name_kz = _name(conn, bin_)
    if not name_ru:
        return None
    params = {"b": bin_, "now": now, "limit": LIST_LIMIT}
    buyer = _rows(conn, """
        SELECT count(*) AS total, coalesce(sum(total_sum), 0) AS amount,
               count(*) FILTER (WHERE end_date > %(now)s) AS open
        FROM announcements WHERE customer_bin = %(b)s OR org_bin = %(b)s""", params)[0]
    open_list = _rows(conn, """
        SELECT id, number_anno, name_ru, name_kz, total_sum, end_date, count_lots
        FROM announcements WHERE (customer_bin = %(b)s OR org_bin = %(b)s) AND end_date > %(now)s
        ORDER BY end_date LIMIT %(limit)s""", params) if buyer["open"] else []
    past_list = _rows(conn, """
        SELECT id, number_anno, name_ru, name_kz, total_sum, end_date, count_lots
        FROM announcements WHERE (customer_bin = %(b)s OR org_bin = %(b)s) AND end_date <= %(now)s
        ORDER BY end_date DESC LIMIT %(limit)s""", params) if buyer["total"] > buyer["open"] else []
    their_winners = _rows(conn, """
        SELECT r.winner_bin AS bin, r.winner_name AS name, count(*) AS wins,
               sum(r.winner_total) AS amount
        FROM lot_results r JOIN lots l ON l.id = r.lot_id
        WHERE l.customer_bin = %(b)s AND r.outcome = 'winner'
        GROUP BY 1, 2 ORDER BY 3 DESC, 4 DESC NULLS LAST LIMIT 10""", params) if buyer["total"] else []
    discount = conn.execute(
        """SELECT avg(1 - r.winner_total / l.amount) FROM lot_results r JOIN lots l ON l.id = r.lot_id
           WHERE l.customer_bin = %s AND r.outcome = 'winner' AND l.amount > 0
             AND r.winner_total BETWEEN 0 AND l.amount""", (bin_,)).fetchone()[0] if buyer["total"] else None
    seller = _rows(conn, """
        SELECT count(*) AS wins, coalesce(sum(winner_total), 0) AS amount
        FROM lot_results WHERE winner_bin = %(b)s AND outcome = 'winner'""", params)[0]
    offered = conn.execute(
        "SELECT count(*) FROM lot_results WHERE offers @> jsonb_build_array(jsonb_build_object('bin', %s::text))",
        (bin_,)).fetchone()[0]
    wins = _rows(conn, """
        SELECT l.id, l.name_ru, l.name_kz, l.amount, r.winner_total, l.customer_bin,
               coalesce(a.customer_name_ru, l.customer_name_ru) AS customer, a.end_date
        FROM lot_results r JOIN lots l ON l.id = r.lot_id LEFT JOIN announcements a ON a.id = l.trd_buy_id
        WHERE r.winner_bin = %(b)s AND r.outcome = 'winner'
        ORDER BY a.end_date DESC NULLS LAST LIMIT %(limit)s""", params) if seller["wins"] else []
    their_customers = _rows(conn, """
        SELECT l.customer_bin AS bin, max(coalesce(a.customer_name_ru, l.customer_name_ru)) AS name,
               count(*) AS wins, sum(r.winner_total) AS amount
        FROM lot_results r JOIN lots l ON l.id = r.lot_id LEFT JOIN announcements a ON a.id = l.trd_buy_id
        WHERE r.winner_bin = %(b)s AND r.outcome = 'winner'
        GROUP BY 1 ORDER BY 3 DESC, 4 DESC NULLS LAST LIMIT 10""", params) if seller["wins"] else []
    # Что покупает (заказчик) и что поставляет (победитель): разделы КПВЭД и частые названия.
    buys_divisions = _rows(conn, """
        SELECT left(enstru[1], 2) AS code, count(*) AS lots, coalesce(sum(amount), 0) AS amount
        FROM lots WHERE customer_bin = %(b)s AND cardinality(enstru) > 0
        GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT 8""", params) if buyer["total"] else []
    buys_names = _rows(conn, """
        SELECT min(name_ru) AS name_ru, min(name_kz) AS name_kz, count(*) AS lots,
               coalesce(sum(amount), 0) AS amount
        FROM lots WHERE customer_bin = %(b)s AND name_ru IS NOT NULL
        GROUP BY lower(name_ru) HAVING count(*) > 1 ORDER BY 3 DESC, 4 DESC LIMIT 12""", params) if buyer["total"] else []
    sells_divisions = _rows(conn, """
        SELECT left(l.enstru[1], 2) AS code, count(*) AS lots, coalesce(sum(r.winner_total), 0) AS amount
        FROM lot_results r JOIN lots l ON l.id = r.lot_id
        WHERE r.winner_bin = %(b)s AND r.outcome = 'winner' AND cardinality(l.enstru) > 0
        GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT 8""", params) if seller["wins"] else []
    if not (buyer["total"] or seller["wins"] or offered):
        return None
    return {
        "bin": bin_, "name_ru": name_ru, "name_kz": name_kz, "card": subjects.card(conn, bin_),
        "buyer": buyer, "open": open_list, "past": past_list, "their_winners": their_winners,
        "discount": float(discount) if discount is not None else None,
        "seller": seller, "offered": offered, "wins": wins, "their_customers": their_customers,
        "buys_divisions": buys_divisions, "buys_names": buys_names, "sells_divisions": sells_divisions,
    }


def sitemap_bins(conn: psycopg.Connection) -> list[str]:
    """Организации для sitemap: заказчики, организаторы и победители (БИН и ИИН)."""
    return [row[0] for row in conn.execute(
        r"""SELECT b FROM (SELECT customer_bin AS b FROM announcements
                           UNION SELECT org_bin FROM announcements
                           UNION SELECT winner_bin FROM lot_results WHERE outcome = 'winner') x
            WHERE b ~ '^\d{12}$' ORDER BY b""")]


# --- Справочник /orgs: все организации с фильтрами (из org_directory) -----------------

FORMS = {"ip": "ИП", "too": "ТОО", "ao": "АО", "gu": "Госучреждения", "gp": "Госпредприятия",
         "oo": "Общественные объединения", "other": "Другие"}
ROLES = {"": "Все", "buyer": "Заказчики", "seller": "Поставщики"}
SORTS = {"amount": "По сумме закупок и побед", "buys": "Больше закупок", "wins": "Больше побед",
         "open": "Больше открытых закупок", "name": "По названию"}
_ORDER = {"amount": "greatest(buy_amount, win_amount) DESC", "buys": "buys DESC, buy_amount DESC",
          "wins": "wins DESC, win_amount DESC", "open": "open_now DESC, buy_amount DESC",
          "name": "name_ru"}
PAGE_SIZE = 50
MAX_PAGE = 200


def refresh_directory(conn: psycopg.Connection) -> None:
    conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY org_directory")
    conn.commit()


def directory(conn: psycopg.Connection, *, role: str = "", form: str = "", region: str = "", q: str = "",
              sort: str = "amount", page: int = 1) -> tuple[list[dict[str, Any]], int]:
    """Страница справочника и сколько всего подходит. Неизвестные значения фильтров — без фильтра."""
    clauses, params = ["name_ru IS NOT NULL"], {}
    if role == "buyer":
        clauses.append("buys > 0")
    elif role == "seller":
        clauses.append("(wins > 0 OR offers > 0)")
    if form in FORMS:
        clauses.append("form = %(form)s")
        params["form"] = form
    if region:
        clauses.append("region = %(region)s")
        params["region"] = region
    q = " ".join(q.split())[:100]
    if q.isdigit():
        clauses.append("bin LIKE %(bin)s")
        params["bin"] = q + "%"
    elif q:
        clauses.append("(name_ru ILIKE %(q)s OR name_kz ILIKE %(q)s)")
        params["q"] = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where = " AND ".join(clauses)
    total = conn.execute(f"SELECT count(*) FROM org_directory WHERE {where}", params).fetchone()[0]
    page = min(max(page, 1), MAX_PAGE)
    rows = _rows(conn, f"""SELECT bin, name_ru, name_kz, form, region, buys, buy_amount, open_now,
                                  wins, win_amount, offers
                           FROM org_directory WHERE {where}
                           ORDER BY {_ORDER.get(sort, _ORDER['amount'])}, bin
                           LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}""", params)
    return rows, total
