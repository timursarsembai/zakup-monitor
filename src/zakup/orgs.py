"""Открытые страницы организаций /org/<БИН>: заказчик — его закупки и кто у него выигрывает,
поставщик — где участвовал и побеждал. Всё из своей базы (объявления, лоты, итоги,
карточки subjects), портал не трогаем.

Страницы — только у юрлиц (БИН). У ИП вместо БИН — ИИН, а в нём дата рождения человека:
таких страниц не делаем и в поисковики не отдаём, в списках ИП — просто по имени.
"""

import re
from datetime import UTC, datetime
from typing import Any

import psycopg

from zakup import subjects

# БИН: ГГММ, затем тип — 4 (юрлицо-резидент), 5 (нерезидент), 6 (ИП совместное и пр.).
# В ИИН на этом месте первая цифра дня рождения: 0–3.
_COMPANY = re.compile(r"^\d{4}[4-6]\d{7}$")
LIST_LIMIT = 30


def is_company(bin_: str | None) -> bool:
    return bool(bin_ and _COMPANY.match(bin_))


def _rows(conn: psycopg.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
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
        "SELECT winner_name FROM lot_results WHERE winner_bin = %s LIMIT 1", (bin_,)).fetchone()
    return (row[0], None) if row else (None, None)


def profile(conn: psycopg.Connection, bin_: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Всё для страницы организации; None — такой организации у нас нет (или это ИП)."""
    if not is_company(bin_):
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
    if not (buyer["total"] or seller["wins"] or offered):
        return None
    return {
        "bin": bin_, "name_ru": name_ru, "name_kz": name_kz, "card": subjects.card(conn, bin_),
        "buyer": buyer, "open": open_list, "past": past_list, "their_winners": their_winners,
        "discount": float(discount) if discount is not None else None,
        "seller": seller, "offered": offered, "wins": wins, "their_customers": their_customers,
    }


def sitemap_bins(conn: psycopg.Connection) -> list[str]:
    """Организации для sitemap: заказчики, организаторы и победители (только юрлица)."""
    return [row[0] for row in conn.execute(
        r"""SELECT b FROM (SELECT customer_bin AS b FROM announcements
                           UNION SELECT org_bin FROM announcements
                           UNION SELECT winner_bin FROM lot_results WHERE outcome = 'winner') x
            WHERE b ~ '^\d{4}[4-6]\d{7}$' ORDER BY b""")]
