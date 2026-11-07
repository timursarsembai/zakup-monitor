"""Главная страница — общая аналитика рынка госзакупок по нашей базе.

Цифры одинаковы для всех и тяжёлые (агрегаты по десяткам тысяч лотов),
поэтому кэшируются на 5 минут и пересчитываются в фоне.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from zakup.filters import METHODS, OPEN_STATUSES, REGIONS, SUBJECTS
from zakup.goszakup import PORTAL_TZ

MARKET_TTL = 300
_market_cache: tuple[float, dict[str, Any]] | None = None


def market_cached() -> tuple[dict[str, Any] | None, bool]:
    """(данные из кэша или None, нужно ли пересчитать)."""
    if _market_cache is None:
        return None, True
    return _market_cache[1], _market_cache[0] <= time.monotonic()


OPEN = "a.end_date > now() AND a.status_id = ANY(%(open)s)"
CUSTOMER = "coalesce(nullif(a.customer_name_ru, ''), nullif(l.customer_name_ru, ''), a.org_name_ru)"


def _rows(conn, sql: str, params: dict | None = None) -> list:
    return conn.execute(sql, {"open": OPEN_STATUSES, **(params or {})}).fetchall()


def market(conn: psycopg.Connection, now: datetime | None = None) -> dict[str, Any]:
    """Общая аналитика по рынку (одинакова для всех) и кладёт её в кэш."""
    global _market_cache
    now = now or datetime.now(UTC)
    today = now.astimezone(PORTAL_TZ).date()
    data: dict[str, Any] = {"computed_at": now}

    # --- Сейчас ---
    data["open"] = _rows(conn, f"SELECT count(*), coalesce(sum(total_sum), 0) FROM announcements a WHERE {OPEN}")[0]
    data["open_lots"] = _rows(conn, f"""
        SELECT count(*), coalesce(sum(l.amount), 0),
               count(*) FILTER (WHERE l.prepayment > 0), count(*) FILTER (WHERE l.prepayment >= 30),
               count(*) FILTER (WHERE a.disable_person),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY l.amount)
        FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
        WHERE {OPEN} AND l.status_id = ANY(%(open)s)""")[0]
    data["subjects"] = [(SUBJECTS.get(sid, "Прочие"), c, t) for sid, c, t in _rows(conn, f"""
        SELECT subject_type_id, count(*), coalesce(sum(total_sum), 0) FROM announcements a
        WHERE {OPEN} GROUP BY 1 ORDER BY 3 DESC""")]
    method_of = {i: name for name, ids in METHODS.values() for i in ids}
    methods: dict[str, list] = {}
    for method_id, count, total, days in _rows(conn, f"""
            SELECT trade_method_id, count(*), coalesce(sum(total_sum), 0),
                   avg(extract(epoch FROM end_date - publish_date) / 86400)
            FROM announcements a WHERE {OPEN} GROUP BY 1"""):
        entry = methods.setdefault(method_of.get(method_id, "Прочие"), [0, 0, 0.0])
        entry[2] = (entry[2] * entry[0] + float(days or 0) * count) / (entry[0] + count)
        entry[0] += count
        entry[1] += total
    data["methods"] = sorted(((n, c, t, d) for n, (c, t, d) in methods.items()), key=lambda m: -m[1])
    ending = dict(_rows(conn, f"""
        SELECT (a.end_date AT TIME ZONE 'Asia/Almaty')::date, count(*)
        FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
        WHERE {OPEN} AND l.status_id = ANY(%(open)s) AND a.end_date < %(horizon)s GROUP BY 1""",
        {"horizon": now + timedelta(days=8)}))
    data["ending"] = [(today + timedelta(days=i), ending.get(today + timedelta(days=i), 0)) for i in range(8)]
    data["ending_max"] = max((c for _, c in data["ending"]), default=0) or 1

    # --- Динамика ---
    since = datetime.combine(today - timedelta(days=13), datetime.min.time(), PORTAL_TZ)
    by_day = {d: (c, s) for d, c, s in _rows(conn, """
        SELECT (publish_date AT TIME ZONE 'Asia/Almaty')::date, count(*), coalesce(sum(total_sum), 0)
        FROM announcements WHERE publish_date >= %(since)s GROUP BY 1""", {"since": since})}
    data["days"] = [(today - timedelta(days=i), *by_day.get(today - timedelta(days=i), (0, 0)))
                    for i in range(13, -1, -1)]
    start_of_today = datetime.combine(today, datetime.min.time(), PORTAL_TZ)
    # Первый полноценный день сбора: раньше в базе лишь единичные объявления,
    # и они занизили бы средние по дням недели.
    full_since = _rows(conn, """
        SELECT min(day) FROM (
            SELECT (publish_date AT TIME ZONE 'Asia/Almaty')::date AS day FROM announcements
            GROUP BY 1 HAVING count(*) >= 500) busy""")[0][0] or today
    data["since"] = full_since
    data["days"] = [day for day in data["days"] if day[0] >= full_since] or data["days"]
    data["days_max"] = max((c for _, c, _ in data["days"]), default=0) or 1
    weekdays = {int(d): (float(c), float(t)) for d, c, t in _rows(conn, """
        SELECT extract(isodow FROM day), avg(n), avg(total) FROM (
            SELECT (publish_date AT TIME ZONE 'Asia/Almaty')::date AS day, count(*) AS n,
                   coalesce(sum(total_sum), 0) AS total
            FROM announcements WHERE publish_date < %(today)s
              AND publish_date >= greatest(%(today)s - interval '56 days', %(full)s)
            GROUP BY 1) per_day GROUP BY 1""",
        {"today": start_of_today, "full": datetime.combine(full_since, datetime.min.time(), PORTAL_TZ)})}
    data["weekdays"] = [(i, *weekdays.get(i, (0.0, 0.0))) for i in range(1, 8)]
    data["weekdays_max"] = max((c for _, c, _ in data["weekdays"]), default=0) or 1
    hours = dict(_rows(conn, """
        SELECT extract(hour FROM publish_date AT TIME ZONE 'Asia/Almaty')::int, count(*)
        FROM announcements WHERE publish_date >= %(today)s - interval '28 days' GROUP BY 1""",
        {"today": start_of_today}))
    data["hours"] = [(h, hours.get(h, 0)) for h in range(24)]
    data["hours_max"] = max(hours.values(), default=0) or 1

    # --- Регионы ---
    regions = _rows(conn, f"""
        SELECT left(k, 2), count(DISTINCT l.id), coalesce(sum(l.amount), 0),
               count(DISTINCT l.id) FILTER (WHERE l.prepayment > 0)
        FROM lots l JOIN announcements a ON a.id = l.trd_buy_id,
             unnest(coalesce(nullif(l.kato, '{{}}'), a.kato)) k
        WHERE {OPEN} AND l.status_id = ANY(%(open)s) GROUP BY 1""")
    competition = {code: (w, f, o) for code, w, f, o in _rows(conn, """
        SELECT left(k, 2), count(DISTINCT r.lot_id) FILTER (WHERE r.outcome = 'winner'),
               count(DISTINCT r.lot_id) FILTER (WHERE r.outcome = 'failed'),
               avg(jsonb_array_length(r.offers)) FILTER (WHERE r.outcome = 'winner')
        FROM lot_results r JOIN lots l ON l.id = r.lot_id JOIN announcements a ON a.id = l.trd_buy_id,
             unnest(coalesce(nullif(l.kato, '{}'), a.kato)) k
        GROUP BY 1""")}
    data["regions"] = sorted(
        ((REGIONS[code], count, total, total / count if count else 0, with_advance / count if count else 0,
          *competition.get(code, (0, 0, None)))
         for code, count, total, with_advance in regions if code in REGIONS),
        key=lambda r: -r[2])
    data["regions_max"] = max((r[2] for r in data["regions"]), default=0) or 1

    # --- Крупнейшие ---
    data["top_lots"] = _rows(conn, f"""
        SELECT l.id, l.name_ru, l.name_kz, l.amount, {CUSTOMER}, a.end_date, l.prepayment
        FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
        WHERE {OPEN} AND l.status_id = ANY(%(open)s) ORDER BY l.amount DESC NULLS LAST LIMIT 10""")
    data["top_announcements"] = _rows(conn, f"""
        SELECT a.id, a.name_ru, a.name_kz, a.total_sum, a.count_lots,
               coalesce(nullif(a.customer_name_ru, ''), nullif(a.org_name_ru, ''),
                        (SELECT l.customer_name_ru FROM lots l WHERE l.trd_buy_id = a.id
                           AND l.customer_name_ru <> '' LIMIT 1)), a.end_date
        FROM announcements a WHERE {OPEN} ORDER BY a.total_sum DESC NULLS LAST LIMIT 10""")
    data["top_customers"] = _rows(conn, f"""
        SELECT {CUSTOMER} AS customer, count(DISTINCT a.id), count(*), sum(l.amount)
        FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
        WHERE {OPEN} AND l.status_id = ANY(%(open)s)
        GROUP BY 1 HAVING {CUSTOMER} IS NOT NULL ORDER BY 4 DESC NULLS LAST LIMIT 10""")

    # --- Конкуренция (итоги из протоколов) ---
    data["results"] = _rows(conn, """
        SELECT count(*) FILTER (WHERE r.outcome = 'winner'), count(*) FILTER (WHERE r.outcome = 'failed'),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY 1 - r.winner_total / l.amount)
                   FILTER (WHERE r.outcome = 'winner' AND l.amount > 0 AND r.winner_total <= l.amount),
               avg(jsonb_array_length(r.offers)) FILTER (WHERE r.outcome = 'winner'),
               coalesce(sum(l.amount - r.winner_total) FILTER (WHERE r.outcome = 'winner'
                   AND r.winner_total <= l.amount), 0)
        FROM lot_results r JOIN lots l ON l.id = r.lot_id""")[0]
    offers = dict(_rows(conn, """
        SELECT least(jsonb_array_length(offers), 6), count(*) FROM lot_results
        WHERE outcome = 'winner' GROUP BY 1"""))
    data["offers"] = [(n, offers.get(n, 0)) for n in range(1, 7)]
    data["offers_max"] = max(offers.values(), default=0) or 1
    data["fail_reasons"] = _rows(conn, """
        SELECT coalesce(reason, 'причина не указана'), count(*) FROM lot_results
        WHERE outcome = 'failed' GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
    data["discount_by_method"] = sorted(
        ((method_of.get(m, "Прочие"), n, float(d or 0)) for m, n, d in _rows(conn, """
            SELECT a.trade_method_id, count(*),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY 1 - r.winner_total / l.amount)
            FROM lot_results r JOIN lots l ON l.id = r.lot_id JOIN announcements a ON a.id = l.trd_buy_id
            WHERE r.outcome = 'winner' AND l.amount > 0 AND r.winner_total <= l.amount GROUP BY 1""")),
        key=lambda m: -m[1])
    data["top_winners"] = _rows(conn, """
        SELECT winner_name, count(*), sum(winner_total) FROM lot_results
        WHERE outcome = 'winner' AND winner_name IS NOT NULL
        GROUP BY winner_bin, winner_name ORDER BY 2 DESC, 3 DESC LIMIT 10""")

    # --- Что покупают ---
    data["top_names"] = _rows(conn, """
        SELECT min(l.name_ru), min(l.name_kz), count(*), sum(l.amount), count(DISTINCT l.customer_bin)
        FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
        WHERE a.publish_date >= %(since)s AND l.name_ru IS NOT NULL
        GROUP BY lower(trim(l.name_ru)) ORDER BY 3 DESC LIMIT 15""",
        {"since": now - timedelta(days=14)})
    data["top_names_sum"] = _rows(conn, """
        SELECT min(l.name_ru), min(l.name_kz), count(*), sum(l.amount)
        FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
        WHERE a.publish_date >= %(since)s AND l.name_ru IS NOT NULL
        GROUP BY lower(trim(l.name_ru)) ORDER BY 4 DESC NULLS LAST LIMIT 15""",
        {"since": now - timedelta(days=14)})

    data["freshness"] = _rows(conn, "SELECT max(updated_at) FROM sync_state")[0]
    _market_cache = (time.monotonic() + MARKET_TTL, data)
    return data
