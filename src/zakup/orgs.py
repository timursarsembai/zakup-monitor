"""Открытые страницы организаций /org/<БИН>: заказчик — его закупки и кто у него выигрывает,
поставщик — где участвовал и побеждал. Всё из своей базы (объявления, лоты, итоги,
карточки subjects), портал не трогаем.

Страницы — у всех участников с БИН или ИИН (12 цифр), и у ИП тоже: это открытые данные
портала (решение владельца 2026-11-09).
"""

import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from zakup import subjects
from zakup.goszakup import PORTAL_TZ

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
    """Название: из карточки портала, иначе как заказчика (лот, объявление), победителя или
    участника. org_name_ru в объявлениях портал не заполняет — его не берём."""
    row = conn.execute("SELECT name_ru, name_kz FROM subjects WHERE bin = %s AND name_ru IS NOT NULL",
                       (bin_,)).fetchone()
    if row:
        return row[0], row[1]
    for sql in (
        "SELECT customer_name_ru FROM lots WHERE customer_bin = %(b)s AND customer_name_ru IS NOT NULL LIMIT 1",
        "SELECT customer_name_ru FROM announcements WHERE customer_bin = %(b)s AND customer_name_ru IS NOT NULL LIMIT 1",
        "SELECT winner_name FROM lot_results WHERE winner_bin = %(b)s AND winner_name IS NOT NULL LIMIT 1",
        # Только подавал заявки — имя из списка заявок.
        """SELECT o->>'name' FROM lot_results r, jsonb_array_elements(r.offers) o
           WHERE r.offers @> jsonb_build_array(jsonb_build_object('bin', %(b)s::text)) AND o->>'bin' = %(b)s
             AND o->>'name' IS NOT NULL LIMIT 1""",
    ):
        row = conn.execute(sql, {"b": bin_}).fetchone()
        if row and row[0]:
            return row[0], None
    return None, None


# Объявления организации как заказчика или организатора, в том числе где она — заказчик
# отдельного лота (совместные закупки). Так же считает справочник (org_directory).
_MINE = """(SELECT id FROM announcements WHERE customer_bin = %(b)s OR org_bin = %(b)s
            UNION SELECT trd_buy_id FROM lots WHERE customer_bin = %(b)s)"""


# Вкладки страницы организации: (ключ, название). Показываются только те, где есть данные.
TABS = (("overview", "Обзор"), ("wins", "Победы"), ("history", "История участия"),
        ("where", "Где может участвовать"), ("buys", "Закупки"))
TAB_PAGE = 50
WIN_SORTS = {"date": "a.end_date DESC NULLS LAST, l.id", "amount": "r.winner_total DESC NULLS LAST, l.id",
             "lot": "l.name_ru, l.id", "customer": "customer, l.id"}
RESULTS = {"win": "Победа", "second": "Второе место", "lost": "Не выиграл", "failed": "Не состоялась"}
DYNAMICS_DAYS = 30


def _offered_sql() -> str:
    return "r.offers @> jsonb_build_array(jsonb_build_object('bin', %(b)s::text))"


def profile(conn: psycopg.Connection, bin_: str, tab: str = "overview", args: dict | None = None,
            now: datetime | None = None, paid: bool = True) -> dict[str, Any] | None:
    """Страница организации: шапка и цифры — всегда, данные — только открытой вкладки.
    None — такой организации у нас нет."""
    if not has_page(bin_):
        return None
    args = args or {}
    now = now or datetime.now(UTC)
    name_ru, name_kz = _name(conn, bin_)
    if not name_ru:
        return None
    params = {"b": bin_, "now": now, "limit": LIST_LIMIT}
    buyer = _rows(conn, """
        SELECT count(*) AS total, coalesce(sum(total_sum), 0) AS amount,
               count(*) FILTER (WHERE end_date > %(now)s) AS open
        FROM announcements WHERE id IN """ + _MINE, params)[0]
    seller = _rows(conn, """
        SELECT count(*) AS wins, coalesce(sum(winner_total), 0) AS amount
        FROM lot_results WHERE winner_bin = %(b)s AND outcome = 'winner'""", params)[0]
    offered = conn.execute(f"SELECT count(*) FROM lot_results r WHERE {_offered_sql()}", params).fetchone()[0]
    if not (buyer["total"] or seller["wins"] or offered):
        return None
    has_codes = bool(offered) and conn.execute(
        "SELECT 1 FROM supplier_activity WHERE bin = %s AND code <> '' LIMIT 1", (bin_,)).fetchone() is not None
    tabs = [key for key, _ in TABS if key == "overview" or (key == "wins" and seller["wins"])
            or (key == "history" and offered) or (key == "where" and has_codes) or (key == "buys" and buyer["total"])]
    tab = tab if tab in tabs else "overview"
    # paid — снижение цены поставщиком и суммы его заявок только по тарифу (как на странице лота);
    # отрезается здесь, а не скрывается в вёрстке.
    data = {"bin": bin_, "name_ru": name_ru, "name_kz": name_kz, "card": subjects.card(conn, bin_),
            "buyer": buyer, "seller": seller, "offered": offered, "tabs": tabs, "tab": tab, "paid": paid}
    data["discount"] = conn.execute(
        """SELECT avg(1 - r.winner_total / l.amount) FROM lot_results r JOIN lots l ON l.id = r.lot_id
           WHERE l.customer_bin = %s AND r.outcome = 'winner' AND l.amount > 0
             AND r.winner_total BETWEEN 0 AND l.amount""", (bin_,)).fetchone()[0] if buyer["total"] else None
    data["seller_discount"] = conn.execute(
        """SELECT avg(1 - r.winner_total / l.amount) FROM lot_results r JOIN lots l ON l.id = r.lot_id
           WHERE r.winner_bin = %s AND r.outcome = 'winner' AND l.amount > 0
             AND r.winner_total BETWEEN 0 AND l.amount""", (bin_,)).fetchone()[0] if seller["wins"] and paid else None
    data.update({"overview": _overview, "wins": _wins, "history": _history, "where": _where,
                 "buys": _buys}[tab](conn, params, data, args, now))
    return data


def _overview(conn, params, data, args, now) -> dict[str, Any]:
    out: dict[str, Any] = {}
    since = now - timedelta(days=DYNAMICS_DAYS)
    days = [(since + timedelta(days=i + 1)).astimezone(PORTAL_TZ).date() for i in range(DYNAMICS_DAYS)]
    if data["offered"]:
        # Заявки и победы по дням: день — подача заявки (applied_at), иначе окончание приёма.
        rows = conn.execute(
            f"""SELECT coalesce(CASE WHEN o->>'applied_at' ~ '^\\d\\d\\.\\d\\d\\.\\d{{4}} \\d\\d:\\d\\d:\\d\\d$'
                                     THEN to_timestamp(o->>'applied_at', 'DD.MM.YYYY HH24:MI:SS')::date END,
                                (a.end_date AT TIME ZONE 'Asia/Almaty')::date) AS day,
                       count(*), count(*) FILTER (WHERE r.outcome = 'winner' AND r.winner_bin = %(b)s)
                FROM lot_results r CROSS JOIN LATERAL jsonb_array_elements(r.offers) o
                JOIN lots l ON l.id = r.lot_id LEFT JOIN announcements a ON a.id = l.trd_buy_id
                WHERE {_offered_sql()} AND o->>'bin' = %(b)s GROUP BY 1""", params).fetchall()
        by_day = {day: (n, w) for day, n, w in rows}
        out["dynamics"] = [(d, *by_day.get(d, (0, 0))) for d in days]
        out.update(_seller_year(conn, params, now))
    if data["buyer"]["total"]:
        rows = conn.execute(
            """SELECT (publish_date AT TIME ZONE 'Asia/Almaty')::date, count(*) FROM announcements
               WHERE id IN """ + _MINE + " AND publish_date >= %(since)s GROUP BY 1",
            {**params, "since": since}).fetchall()
        by_day = dict(rows)
        out["buy_dynamics"] = [(d, by_day.get(d, 0)) for d in days]
        out["buys_divisions"] = _rows(conn, """
            SELECT left(enstru[1], 2) AS code, count(*) AS lots, coalesce(sum(amount), 0) AS amount
            FROM lots WHERE customer_bin = %(b)s AND cardinality(enstru) > 0
            GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT 8""", params)
        out["buys_names"] = _rows(conn, """
            SELECT min(name_ru) AS name_ru, min(name_kz) AS name_kz, count(*) AS lots,
                   coalesce(sum(amount), 0) AS amount
            FROM lots WHERE customer_bin = %(b)s AND name_ru IS NOT NULL
            GROUP BY lower(name_ru) HAVING count(*) > 1 ORDER BY 3 DESC, 4 DESC LIMIT 12""", params)
        out["their_winners"] = _their_winners(conn, params, 5)
    if data["seller"]["wins"] and not out.get("win_divisions"):  # за год пусто — за всё время
        out["sells_divisions"] = _rows(conn, """
            SELECT left(l.enstru[1], 2) AS code, count(*) AS lots, coalesce(sum(r.winner_total), 0) AS amount
            FROM lot_results r JOIN lots l ON l.id = r.lot_id
            WHERE r.winner_bin = %(b)s AND r.outcome = 'winner' AND cardinality(l.enstru) > 0
            GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT 8""", params)
        out["their_customers"] = _their_customers(conn, params, 5)
    return out


MONTHS = 12
def _seller_year(conn, params, now) -> dict[str, Any]:
    """Поставщик за год: заявки и победы по месяцам (с месяца, с которого у нас есть итоги),
    в чём участвует и что выигрывает — разделы ЕНС ТРУ в процентах, регионы поставки.
    Месяц — по окончанию приёма заявок: дата итогов у догруженных лотов — день загрузки.
    График — с первого месяца, где он участвовал.
    Доли — от всех его лотов, а не от показанных строк."""
    local = now.astimezone(PORTAL_TZ)
    months = []
    y, m = local.year, local.month
    for _ in range(MONTHS):
        months.append(date(y, m, 1))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.sort()
    query = {**params, "since": datetime.combine(months[0], datetime.min.time(), PORTAL_TZ)}
    base = f"""FROM lot_results r JOIN lots l ON l.id = r.lot_id JOIN announcements a ON a.id = l.trd_buy_id
               WHERE {_offered_sql()} AND a.end_date >= %(since)s"""
    won = "r.outcome = 'winner' AND r.winner_bin = %(b)s"
    by_month = {row[0]: row[1:] for row in conn.execute(
        f"""SELECT date_trunc('month', a.end_date AT TIME ZONE 'Asia/Almaty')::date, count(*),
                   count(*) FILTER (WHERE {won}) {base} GROUP BY 1""", query)}
    # С первого месяца, где он участвовал (история в базе неполная — не рисуем пустые месяцы до неё).
    months = [d for d in months if d >= min(by_month, default=months[-1])]
    out: dict[str, Any] = {"months": [(d, *by_month.get(d, (0, 0))) for d in months]}
    out["year"] = dict(zip(("offers", "wins", "amount"), conn.execute(
        f"SELECT count(*), count(*) FILTER (WHERE {won}), coalesce(sum(r.winner_total) FILTER (WHERE {won}), 0) {base}",
        query).fetchone(), strict=True))
    out["year_since"] = months[0]

    def shares(source: str, key: str, condition: str = "true") -> list[dict[str, Any]]:
        rows = _rows(conn, f"""SELECT {key} AS code, count(*) AS lots, count(*) FILTER (WHERE {won}) AS wins,
                                      coalesce(sum(r.winner_total) FILTER (WHERE {won}), 0) AS amount,
                                      sum(count(*)) OVER () AS total
                               {source} AND {condition} GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", query)
        for r in rows:
            r["share"] = r["lots"] / r["total"] if r["total"] else 0
        return rows

    division = "left(l.enstru[1], 2)"
    out["offer_divisions"] = shares(base, division, "cardinality(l.enstru) > 0")
    out["win_divisions"] = shares(base, division, f"cardinality(l.enstru) > 0 AND {won}")
    # Регионы — все регионы поставки лота (lots.regions), как в фильтрах; лот в двух регионах —
    # в обоих.
    out["offer_regions"] = shares(base.replace("WHERE", ", unnest(l.regions) reg WHERE", 1), "reg")
    return out


def _their_winners(conn, params, limit: int) -> list[dict[str, Any]]:
    return _rows(conn, f"""
        SELECT r.winner_bin AS bin, max(r.winner_name) AS name, count(*) AS wins, sum(r.winner_total) AS amount
        FROM lot_results r JOIN lots l ON l.id = r.lot_id
        WHERE l.customer_bin = %(b)s AND r.outcome = 'winner'
        GROUP BY 1 ORDER BY 3 DESC, 4 DESC NULLS LAST LIMIT {int(limit)}""", params)


def _their_customers(conn, params, limit: int) -> list[dict[str, Any]]:
    return _rows(conn, f"""
        SELECT l.customer_bin AS bin, max(coalesce(a.customer_name_ru, l.customer_name_ru)) AS name,
               count(*) AS wins, sum(r.winner_total) AS amount
        FROM lot_results r JOIN lots l ON l.id = r.lot_id LEFT JOIN announcements a ON a.id = l.trd_buy_id
        WHERE r.winner_bin = %(b)s AND r.outcome = 'winner'
        GROUP BY 1 ORDER BY 3 DESC, 4 DESC NULLS LAST LIMIT {int(limit)}""", params)


def _page(args: dict) -> int:
    try:
        return min(max(int(args.get("page") or 1), 1), MAX_PAGE)
    except ValueError:
        return 1


def _wins(conn, params, data, args, now) -> dict[str, Any]:
    sort = args.get("sort") if args.get("sort") in WIN_SORTS else "date"
    customers = _their_customers(conn, params, 200)
    customer = args.get("customer") if args.get("customer") in {c["bin"] for c in customers} else ""
    where = "r.winner_bin = %(b)s AND r.outcome = 'winner'" + (" AND l.customer_bin = %(customer)s" if customer else "")
    page = _page(args)
    query = {**params, "customer": customer}
    total = conn.execute(f"SELECT count(*) FROM lot_results r JOIN lots l ON l.id = r.lot_id WHERE {where}",
                         query).fetchone()[0]
    rows = _rows(conn, f"""
        SELECT l.id, l.name_ru, l.name_kz, l.lot_number, l.amount, r.winner_total, l.customer_bin,
               coalesce(a.customer_name_ru, l.customer_name_ru) AS customer, a.end_date,
               jsonb_array_length(coalesce(r.offers, '[]')) AS bidders
        FROM lot_results r JOIN lots l ON l.id = r.lot_id LEFT JOIN announcements a ON a.id = l.trd_buy_id
        WHERE {where} ORDER BY {WIN_SORTS[sort]}
        LIMIT {TAB_PAGE} OFFSET {(page - 1) * TAB_PAGE}""", query)
    return {"rows": rows, "total": total, "page": page, "pages": min((total + TAB_PAGE - 1) // TAB_PAGE, MAX_PAGE),
            "sort": sort, "customer": customer, "customers": customers}


def _history(conn, params, data, args, now) -> dict[str, Any]:
    result = args.get("result") if args.get("result") in RESULTS else ""
    outcome = """CASE WHEN r.outcome = 'winner' AND r.winner_bin = %(b)s THEN 'win'
                      WHEN r.second_bin = %(b)s THEN 'second'
                      WHEN r.outcome = 'winner' THEN 'lost' ELSE 'failed' END"""
    where = _offered_sql() + (f" AND {outcome} = %(result)s" if result else "")
    query = {**params, "result": result}
    page = _page(args)
    counts = dict(conn.execute(
        f"SELECT {outcome}, count(*) FROM lot_results r WHERE {_offered_sql()} GROUP BY 1", params).fetchall())
    total = sum(counts.values()) if not result else counts.get(result, 0)
    rows = _rows(conn, f"""
        SELECT l.id, l.name_ru, l.name_kz, l.lot_number, l.amount, l.customer_bin,
               coalesce(a.customer_name_ru, l.customer_name_ru) AS customer, a.end_date,
               (SELECT e->>'total' FROM jsonb_array_elements(r.offers) e WHERE e->>'bin' = %(b)s LIMIT 1) AS offer,
               r.winner_bin, r.winner_name, r.winner_total, {outcome} AS result
        FROM lot_results r JOIN lots l ON l.id = r.lot_id LEFT JOIN announcements a ON a.id = l.trd_buy_id
        WHERE {where} ORDER BY a.end_date DESC NULLS LAST, l.id
        LIMIT {TAB_PAGE} OFFSET {(page - 1) * TAB_PAGE}""", query)
    if not data["paid"]:
        for row in rows:
            row["offer"] = None
    return {"rows": rows, "total": total, "page": page, "pages": min((total + TAB_PAGE - 1) // TAB_PAGE, MAX_PAGE),
            "result": result, "counts": counts}


def _where(conn, params, data, args, now) -> dict[str, Any]:
    """Открытые лоты тех классов товаров (6 цифр ЕНС ТРУ), где организация уже участвовала;
    сначала — в регионах, куда она поставляла."""
    from zakup.catalog import LOT_REGION, open_filter

    classes = [row[0] for row in conn.execute(
        """SELECT left(code, 6) FROM supplier_activity WHERE bin = %s AND code <> ''
           GROUP BY 1 ORDER BY sum(offers) DESC LIMIT 15""", (data["bin"],))]
    regions = [row[0] for row in conn.execute(
        "SELECT region FROM supplier_activity WHERE bin = %s AND region <> '' GROUP BY 1", (data["bin"],))]
    if not classes:
        return {"rows": [], "classes": [], "regions": regions}
    where, query = open_filter().where(now, "lots")
    rows = _rows(conn, f"""
        SELECT l.id, l.name_ru, l.name_kz, l.lot_number, l.amount, l.customer_bin,
               coalesce(a.customer_name_ru, l.customer_name_ru) AS customer, a.end_date,
               {LOT_REGION} AS region, ({LOT_REGION}) = ANY(%(regions)s) AS familiar
        FROM lots l, announcements a
        WHERE {where} AND left(l.enstru[1], 2) = ANY(%(divisions)s) AND left(l.enstru[1], 6) = ANY(%(classes)s)
        ORDER BY familiar DESC, a.end_date, l.id LIMIT {TAB_PAGE}""",
        {**query, "regions": regions, "classes": classes, "divisions": sorted({c[:2] for c in classes})})
    return {"rows": rows, "classes": classes, "regions": regions}


def _buys(conn, params, data, args, now) -> dict[str, Any]:
    buyer = data["buyer"]
    return {
        "open": _rows(conn, """
            SELECT id, number_anno, name_ru, name_kz, total_sum, end_date, count_lots
            FROM announcements WHERE id IN """ + _MINE + """ AND end_date > %(now)s
            ORDER BY end_date LIMIT %(limit)s""", params) if buyer["open"] else [],
        "past": _rows(conn, """
            SELECT id, number_anno, name_ru, name_kz, total_sum, end_date, count_lots
            FROM announcements WHERE id IN """ + _MINE + """ AND end_date <= %(now)s
            ORDER BY end_date DESC LIMIT %(limit)s""", params) if buyer["total"] > buyer["open"] else [],
        "their_winners": _their_winners(conn, params, 20),
    }


SITEMAP_TTL = 3600
_sitemap: dict[str, Any] = {"at": 0.0, "bins": []}
_sitemap_lock = threading.Lock()


def sitemap_bins(conn: psycopg.Connection) -> list[str]:
    """Организации для sitemap — те же, что в справочнике (с названием, иначе страница 404).
    Раз в час."""
    if _sitemap["at"] > time.monotonic():
        return _sitemap["bins"]
    bins = [row[0] for row in conn.execute(
        "SELECT bin FROM org_directory WHERE name_ru IS NOT NULL ORDER BY bin")]
    with _sitemap_lock:
        _sitemap.update(at=time.monotonic() + SITEMAP_TTL, bins=bins)
    return bins


# --- Справочник /orgs: все организации с фильтрами (из org_directory) -----------------

FORMS = {"ip": "ИП", "too": "ТОО", "ao": "АО", "gu": "Госучреждения", "gp": "Госпредприятия",
         "oo": "Общественные объединения", "other": "Другие"}
ROLES = {"": "Все", "buyer": "Заказчики", "seller": "Поставщики"}
SORTS = {"amount": "По сумме закупок и побед", "buys": "Больше закупок", "wins": "Больше побед",
         "open": "Больше открытых закупок", "name": "По названию"}
# Отдельные страницы заказчиков и поставщиков — свои сортировки (первая — по умолчанию).
ROLE_SORTS = {
    "buyer": {"buy_amount": "Сумма закупок — больше", "buys": "Больше закупок", "open": "Больше открытых закупок",
              "name": "По названию"},
    "seller": {"win_amount": "Сумма побед — больше", "wins": "Больше побед", "offers": "Больше заявок",
               "name": "По названию"},
}
_ORDER = {"amount": "greatest(buy_amount, win_amount) DESC", "buys": "buys DESC, buy_amount DESC",
          "buy_amount": "buy_amount DESC", "win_amount": "win_amount DESC, wins DESC",
          "offers": "offers DESC, wins DESC",
          "wins": "wins DESC, win_amount DESC", "open": "open_now DESC, buy_amount DESC",
          "name": "name_ru"}
PAGE_SIZE = 50
MAX_PAGE = 200


def refresh_directory(conn: psycopg.Connection) -> None:
    """Справочник и «кто что поставлял» — с новыми закупками, итогами и карточками."""
    conn.execute("SET LOCAL work_mem = '64MB'")  # сортировки пересчёта — в памяти, не на диске
    conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY org_directory")
    conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY supplier_activity")
    conn.commit()


_ROLE_SQL = {"": "true", "buyer": "buys > 0", "seller": "(wins > 0 OR offers > 0)"}


def form_counts(conn: psycopg.Connection, role: str = "") -> dict[str, int]:
    """Сколько организаций каждой формы у выбранной роли (у заказчиков нет ИП и т. п.)."""
    rows = conn.execute(f"SELECT form, count(*) FROM org_directory WHERE name_ru IS NOT NULL "
                        f"AND {_ROLE_SQL.get(role, 'true')} GROUP BY form").fetchall()
    return {form: n for form, n in rows if n}


def directory(conn: psycopg.Connection, *, role: str = "", forms: list[str] | None = None,
              regions: list[str] | None = None, q: str = "", sort: str = "amount",
              page: int = 1) -> tuple[list[dict[str, Any]], int]:
    """Страница справочника и сколько всего подходит. Формы и регионы — любой из выбранных;
    неизвестные значения форм отбрасываются."""
    clauses, params = ["name_ru IS NOT NULL", _ROLE_SQL.get(role, "true")], {}
    forms = [f for f in forms or [] if f in FORMS]
    if forms:
        clauses.append("form = ANY(%(forms)s)")
        params["forms"] = forms
    if regions:
        clauses.append("region = ANY(%(regions)s)")
        params["regions"] = list(regions)
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
