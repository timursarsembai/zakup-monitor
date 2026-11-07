"""Данные главной страницы: свои дела пользователя и обзор рынка по базе.

Обзор рынка одинаков для всех и тяжелее (агрегаты по десяткам тысяч лотов),
поэтому кэшируется на 5 минут; личный блок считается на каждый заход.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from zakup.filters import METHODS, OPEN_STATUSES, REGIONS, Filters, deadline_by_rule
from zakup.goszakup import PORTAL_TZ

MARKET_TTL = 300
_market_cache: tuple[float, dict[str, Any]] | None = None


def personal(conn: psycopg.Connection, user_id: int) -> dict[str, Any]:
    counts = dict(conn.execute(
        "SELECT status, count(*) FROM lot_marks WHERE user_id = %s AND status <> '' GROUP BY 1",
        (user_id,),
    ).fetchall())
    upcoming = conn.execute(
        """SELECT l.id, l.name_ru, l.amount, l.prepayment, a.end_date, m.status
           FROM lot_marks m JOIN lots l ON l.id = m.lot_id
           JOIN announcements a ON a.id = l.trd_buy_id
           WHERE m.user_id = %s AND m.status = 'interested'
             AND a.end_date > now()
           ORDER BY a.end_date LIMIT 6""",
        (user_id,),
    ).fetchall()
    subscriptions = conn.execute(
        """SELECT s.id, s.name, s.active,
                  count(m.lot_id) FILTER (WHERE m.matched_at > now() - interval '1 day'),
                  count(m.lot_id) FILTER (WHERE m.matched_at > now() - interval '7 days')
           FROM subscriptions s LEFT JOIN matches m ON m.subscription_id = s.id
           WHERE s.user_id = %s GROUP BY s.id ORDER BY s.id""",
        (user_id,),
    ).fetchall()
    return {"marks": counts, "upcoming": upcoming, "subscriptions": subscriptions}


def market_cached() -> tuple[dict[str, Any] | None, bool]:
    """(данные из кэша или None, нужно ли пересчитать)."""
    if _market_cache is None:
        return None, True
    return _market_cache[1], _market_cache[0] <= time.monotonic()


def market(conn: psycopg.Connection, now: datetime | None = None) -> dict[str, Any]:
    """Пересчитывает обзор рынка (около 2 с) и кладёт в кэш."""
    global _market_cache
    now = now or datetime.now(UTC)
    today = now.astimezone(PORTAL_TZ).date()
    data: dict[str, Any] = {}
    data["open"] = conn.execute(
        """SELECT count(*), coalesce(sum(total_sum), 0) FROM announcements
           WHERE end_date > now() AND status_id = ANY(%s)""",
        (OPEN_STATUSES,),
    ).fetchone()
    data["open_lots"] = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE l.prepayment > 0),
                  count(*) FILTER (WHERE l.prepayment >= 30),
                  count(*) FILTER (WHERE a.disable_person)
           FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
           WHERE a.end_date > now() AND l.status_id = ANY(%s)""",
        (OPEN_STATUSES,),
    ).fetchone()
    days = conn.execute(
        """SELECT (publish_date AT TIME ZONE 'Asia/Almaty')::date, count(*),
                  coalesce(sum(total_sum), 0)
           FROM announcements WHERE publish_date >= %s
           GROUP BY 1 ORDER BY 1""",
        (datetime.combine(today - timedelta(days=13), datetime.min.time(), PORTAL_TZ),),
    ).fetchall()
    by_day = {d: (c, s) for d, c, s in days}
    data["days"] = [(today - timedelta(days=i), *by_day.get(today - timedelta(days=i), (0, 0)))
                    for i in range(13, -1, -1)]
    data["days_max"] = max((c for _, c, _ in data["days"]), default=0) or 1

    # Под фильтр владельца по умолчанию, но без срока: что заканчивается в ближайшие дни.
    default = Filters(deadline="")
    where, params = default.where(now, "lots")
    data["deadlines"] = conn.execute(
        f"""SELECT (a.end_date AT TIME ZONE 'Asia/Almaty')::date, count(*), sum(l.amount)
            FROM lots l, announcements a WHERE {where}
              AND a.end_date < %(horizon)s
            GROUP BY 1 ORDER BY 1""",
        {**params, "horizon": now + timedelta(days=8)},
    ).fetchall()
    data["rule_day"] = deadline_by_rule(today)
    where, params = Filters().where(now, "lots")
    data["default_now"] = conn.execute(
        f"""SELECT count(*), count(DISTINCT a.id), coalesce(sum(l.amount), 0)
            FROM lots l, announcements a WHERE {where}""",
        params,
    ).fetchone()

    regions = conn.execute(
        """SELECT left(k, 2), count(DISTINCT l.id), sum(l.amount)
           FROM lots l JOIN announcements a ON a.id = l.trd_buy_id,
                unnest(coalesce(nullif(l.kato, '{}'), a.kato)) k
           WHERE a.end_date > now() AND l.status_id = ANY(%s)
           GROUP BY 1 ORDER BY 2 DESC""",
        (OPEN_STATUSES,),
    ).fetchall()
    data["regions"] = [(REGIONS.get(code, code), count, total) for code, count, total in regions
                       if code in REGIONS][:8]
    data["regions_max"] = max((c for _, c, _ in data["regions"]), default=0) or 1

    method_of = {i: name for name, ids in METHODS.values() for i in ids}
    methods: dict[str, list] = {}
    for method_id, count, total in conn.execute(
        """SELECT trade_method_id, count(*), coalesce(sum(total_sum), 0) FROM announcements
           WHERE end_date > now() AND status_id = ANY(%s) GROUP BY 1""",
        (OPEN_STATUSES,),
    ):
        name = method_of.get(method_id, "Прочие")
        entry = methods.setdefault(name, [0, 0])
        entry[0] += count
        entry[1] += total
    data["methods"] = sorted(((n, c, t) for n, (c, t) in methods.items()), key=lambda m: -m[1])

    data["results"] = conn.execute(
        """SELECT count(*) FILTER (WHERE r.outcome = 'winner'),
                  count(*) FILTER (WHERE r.outcome = 'failed'),
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY 1 - r.winner_total / l.amount)
                      FILTER (WHERE r.outcome = 'winner' AND l.amount > 0
                              AND r.winner_total <= l.amount),
                  avg(jsonb_array_length(r.offers)) FILTER (WHERE r.outcome = 'winner')
           FROM lot_results r JOIN lots l ON l.id = r.lot_id
           WHERE r.parsed_at > now() - interval '30 days'"""
    ).fetchone()
    data["lot_outcomes"] = conn.execute(
        """SELECT count(*) FILTER (WHERE l.status_id = 360), count(*) FILTER (WHERE l.status_id = 370)
           FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
           WHERE a.end_date > now() - interval '7 days' AND a.end_date <= now()"""
    ).fetchone()
    data["freshness"] = conn.execute(
        """SELECT (SELECT max(updated_at) FROM sync_state),
                  (SELECT count(*) FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
                   WHERE a.end_date > now() AND l.spec_url IS NOT NULL),
                  (SELECT count(*) FROM lot_specs s JOIN lots l ON l.id = s.lot_id
                   JOIN announcements a ON a.id = l.trd_buy_id
                   WHERE a.end_date > now() AND s.text IS NOT NULL)"""
    ).fetchone()
    data["computed_at"] = now
    _market_cache = (time.monotonic() + MARKET_TTL, data)
    return data
