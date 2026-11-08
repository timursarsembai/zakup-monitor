"""Дашборд администратора: поток закупок, пользователи, деньги, ИИ — за выбранный период.

Отдаёт готовые ряды для графиков (по дням, за год — по неделям) и сводку с
сравнением с предыдущим таким же периодом. Даты — по времени Казахстана.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from zakup.audit import ACTIONS
from zakup.goszakup import PORTAL_TZ

PERIODS = (7, 30, 90, 365)
TZ = "Asia/Almaty"
# Клиенты — не сотрудники; удалённых не считаем.
CLIENT = "u.role = 'user' AND u.deleted_at IS NULL"
# Себестоимость поиска в тенге (для маржи): курс с запасом.
USD_KZT = 520

# (ключ, подпись, выражение-время, таблица с условием). Всё считается одним видом
# запроса: сколько событий (или сумма) попало в каждый день/неделю.
SERIES = {
    "announcements": ("count(*)", "announcements a", "a.publish_date", "true"),
    "lots": ("count(*)", "lots l JOIN announcements a ON a.id = l.trd_buy_id", "a.publish_date", "true"),
    "amount": ("coalesce(sum(a.total_sum), 0) / 1e6", "announcements a", "a.publish_date", "true"),
    "users": ("count(*)", "users u", "u.created_at", CLIENT),
    "revenue": ("coalesce(sum(p.amount), 0)", "payments p", "p.paid_at", "p.status = 'paid'"),
    "searches": ("count(*)", "lot_research r", "r.created_at", "true"),
    "active": ("count(DISTINCT x.user_id)", "user_actions x JOIN users u ON u.id = x.user_id",
               "x.at", CLIENT),
    "views": ("count(*)", "user_actions x", "x.at", "x.action LIKE 'view_%%'"),
}


def _bucket(unit: str, column: str) -> str:
    return f"date_trunc('{unit}', {column} AT TIME ZONE '{TZ}')::date"


def _series(conn: psycopg.Connection, key: str, unit: str, start: date, end: date) -> list[float]:
    value, source, column, where = SERIES[key]
    rows = conn.execute(
        f"""SELECT {_bucket(unit, column)} AS b, {value} FROM {source}
            WHERE {where} AND {column} >= %(from)s AND {column} < %(to)s GROUP BY 1""",
        {"from": _start_ts(start), "to": _start_ts(end)},
    ).fetchall()
    got = {b: float(v or 0) for b, v in rows}
    return [round(got.get(b, 0), 2) for b in _buckets(unit, start, end)]


def _start_ts(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=PORTAL_TZ)


def _buckets(unit: str, start: date, end: date) -> list[date]:
    out, day = [], start
    if unit == "week":
        day = start - timedelta(days=start.weekday())
    step = timedelta(days=7 if unit == "week" else 1)
    while day < end:
        out.append(day)
        day += step
    return out


def _weekdays(conn: psycopg.Connection, key: str, start: date, end: date) -> list[float]:
    """Среднее за день недели (пн…вс): сумма по дню недели / сколько таких дней в периоде.
    start — не раньше начала истории, иначе пустые дни до неё занижают среднее."""
    value, source, column, where = SERIES[key]
    rows = dict(conn.execute(
        f"""SELECT extract(isodow FROM {column} AT TIME ZONE '{TZ}')::int, {value} FROM {source}
            WHERE {where} AND {column} >= %(from)s AND {column} < %(to)s GROUP BY 1""",
        {"from": _start_ts(start), "to": _start_ts(end)},
    ).fetchall())
    days = [0] * 7
    day = start
    while day < end:
        days[day.weekday()] += 1
        day += timedelta(days=1)
    return [round(float(rows.get(i + 1) or 0) / max(days[i], 1), 1) for i in range(7)]


def _hours(conn: psycopg.Connection, start: date, end: date) -> list[int]:
    rows = dict(conn.execute(
        f"""SELECT extract(hour FROM publish_date AT TIME ZONE '{TZ}')::int, count(*)
            FROM announcements WHERE publish_date >= %s AND publish_date < %s GROUP BY 1""",
        (_start_ts(start), _start_ts(end)),
    ).fetchall())
    return [int(rows.get(h) or 0) for h in range(24)]


def _one(conn: psycopg.Connection, sql: str, params: dict | None = None) -> Any:
    return conn.execute(sql, params or {}).fetchone()[0]


def _totals(conn: psycopg.Connection, start: date, end: date) -> dict[str, float]:
    out = {}
    for key, (value, source, column, where) in SERIES.items():
        out[key] = float(_one(conn, f"SELECT {value} FROM {source} WHERE {where} "
                                    f"AND {column} >= %(from)s AND {column} < %(to)s",
                              {"from": _start_ts(start), "to": _start_ts(end)}) or 0)
    return out


def stats(conn: psycopg.Connection, days: int, now: datetime | None = None) -> dict[str, Any]:
    days = days if days in PERIODS else 30
    now = now or datetime.now(UTC)
    end = now.astimezone(PORTAL_TZ).date() + timedelta(days=1)  # сегодня включительно
    start = end - timedelta(days=days)
    unit = "week" if days > 90 else "day"
    period = {"from": _start_ts(start), "to": _start_ts(end)}

    totals = _totals(conn, start, end)
    before = _totals(conn, start - timedelta(days=days), start)
    first = conn.execute("SELECT min(publish_date) FROM announcements").fetchone()[0]
    history = first.astimezone(PORTAL_TZ).date() if first else start

    searches = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE status = 'error'),
                  avg((usage->>'cost_usd')::numeric) FILTER (WHERE usage ? 'cost_usd')
           FROM lot_research WHERE created_at >= %(from)s AND created_at < %(to)s""", period,
    ).fetchone()
    paid_kinds = dict(conn.execute(
        """SELECT kind, coalesce(sum(amount), 0) FROM payments
           WHERE status = 'paid' AND paid_at >= %(from)s AND paid_at < %(to)s GROUP BY 1""", period,
    ).fetchall())
    clients = _one(conn, f"SELECT count(*) FROM users u WHERE {CLIENT}")
    paying = _one(conn, f"SELECT count(*) FROM users u WHERE {CLIENT} AND u.paid_until > now()")
    ever_paid = _one(conn, f"""SELECT count(DISTINCT p.user_id) FROM payments p JOIN users u ON u.id = p.user_id
                               WHERE p.status = 'paid' AND {CLIENT}""")
    with_org = _one(conn, f"""SELECT count(*) FROM users u WHERE {CLIENT}
                              AND EXISTS (SELECT 1 FROM client_orgs o WHERE o.user_id = u.id)""")
    telegram = _one(conn, f"SELECT count(*) FROM users u WHERE {CLIENT} AND u.telegram_chat_id IS NOT NULL")
    expiring = _one(conn, f"""SELECT count(*) FROM users u WHERE {CLIENT}
                              AND u.paid_until > now() AND u.paid_until < now() + interval '7 days'""")
    subs = _one(conn, "SELECT count(*) FROM subscriptions WHERE active")
    sent = _one(conn, "SELECT count(*) FROM matches WHERE notified_at >= %(from)s AND notified_at < %(to)s",
                period)
    actions = conn.execute(
        """SELECT x.action, count(*) FROM user_actions x JOIN users u ON u.id = x.user_id
           WHERE u.role = 'user' AND x.at >= %(from)s AND x.at < %(to)s
           GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", period,
    ).fetchall()
    top_customers = conn.execute(
        """SELECT coalesce(nullif(a.customer_name_ru, ''), a.org_name_ru), count(*),
                  coalesce(sum(a.total_sum), 0)
           FROM announcements a WHERE a.publish_date >= %(from)s AND a.publish_date < %(to)s
           GROUP BY 1 ORDER BY 3 DESC LIMIT 10""", period,
    ).fetchall()
    cost_usd = float(searches[2]) if searches[2] is not None else None
    return {
        "days": days, "unit": unit, "from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(),
        "history_from": history.isoformat(),
        "labels": [b.isoformat() for b in _buckets(unit, start, end)],
        "series": {key: _series(conn, key, unit, start, end) for key in SERIES},
        "weekdays": {key: _weekdays(conn, key, max(start, history), end)
                     for key in ("announcements", "lots", "users", "active")},
        "hours": _hours(conn, start, end),
        "totals": totals,
        "before": before,
        "kpi": {
            "clients": clients, "paying": paying, "ever_paid": ever_paid, "with_org": with_org,
            "telegram": telegram, "expiring": expiring, "subscriptions": subs, "notified": sent,
            "searches": searches[0], "search_errors": searches[1],
            "search_cost_kzt": round(cost_usd * USD_KZT) if cost_usd is not None else None,
            "revenue_tariff": int(paid_kinds.get("tariff", 0)),
            "revenue_searches": int(paid_kinds.get("searches", 0)),
        },
        "actions": [[ACTIONS.get(a, a), c] for a, c in actions],
        "customers": [[n or "—", c, float(t)] for n, c, t in top_customers],
    }
