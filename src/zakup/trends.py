"""Динамика рынка закупок для «Анализа рынка»: за 7/30/90 дней или год.

Страница открыта всем (и поисковым роботам), поэтому результат кэшируется:
пересчёт — не чаще раза в CACHE_TTL на каждый период.
"""

import threading
import time
from datetime import UTC, datetime
from typing import Any

import psycopg

from zakup import stats
from zakup.stats import Source

CACHE_TTL = 600

SOURCES = {
    "announcements": Source("count(*)", "announcements a", "a.publish_date"),
    "lots": Source("count(*)", "lots l JOIN announcements a ON a.id = l.trd_buy_id", "a.publish_date"),
    "amount": Source("coalesce(sum(a.total_sum), 0) / 1e6", "announcements a", "a.publish_date"),
}

_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def history_start(conn: psycopg.Connection):
    """Первый полноценный день сбора: раньше в базе лишь единичные объявления."""
    return conn.execute(
        f"""SELECT min(day) FROM (SELECT (publish_date AT TIME ZONE '{stats.TZ}')::date AS day
            FROM announcements GROUP BY 1 HAVING count(*) >= 500) busy"""
    ).fetchone()[0]


def compute(conn: psycopg.Connection, days: int, now: datetime | None = None) -> dict[str, Any]:
    p = stats.period(days, now or datetime.now(UTC))
    since = history_start(conn) or p.start
    data = stats.pack(conn, SOURCES, p)
    data.update(
        days=p.days, unit=p.unit, start=p.start.isoformat(), end=p.end.isoformat(),
        since=since.isoformat(),
        weekdays={key: stats.weekdays(conn, SOURCES[key], max(p.start, since), p.end)
                  for key in ("announcements", "lots")},
        hours=stats.hours(conn, SOURCES["announcements"], p),
        customers=[[name or "—", count, float(amount)] for name, count, amount in conn.execute(
            """SELECT coalesce(nullif(a.customer_name_ru, ''), a.org_name_ru), count(*),
                      coalesce(sum(a.total_sum), 0)
               FROM announcements a WHERE a.publish_date >= %(from)s AND a.publish_date < %(to)s
               GROUP BY 1 ORDER BY 3 DESC LIMIT 10""", p.bounds).fetchall()],
    )
    return data


def cached(conn_factory, days: int) -> dict[str, Any]:
    days = days if days in stats.PERIODS else 30
    hit = _cache.get(days)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    with _lock:  # один пересчёт за раз: обход роботами не положит базу
        hit = _cache.get(days)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        with conn_factory() as conn:
            data = compute(conn, days)
        _cache[days] = (time.monotonic() + CACHE_TTL, data)
        return data
