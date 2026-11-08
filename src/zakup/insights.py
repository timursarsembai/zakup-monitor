"""Дашборд платформы (администратор и модераторы): посетители, воронка, функции, поиск,
удержание, деньги, клиенты, состояние сервиса — за выбранный период.

Каждый раздел — своя функция, страница грузит их по мере открытия вкладок
(/dashboard/data?section=…&days=…). Клиенты — пользователи с ролью user (не сотрудники).
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from zakup import stats, tariff
from zakup.audit import ACTIONS
from zakup.filters import METHODS, REGIONS, SUBJECTS
from zakup.payments import SEARCH_PRICE
from zakup.stats import Period, Source

CLIENT = "u.role = 'user' AND u.deleted_at IS NULL"
HUMAN = "NOT v.is_bot"
USD_KZT = 520  # для себестоимости ИИ в тенге (с запасом)

SOURCES = {
    "visitors": Source("count(DISTINCT v.visitor)", "visits v", "v.at", HUMAN),
    "views": Source("count(*)", "visits v", "v.at", HUMAN),
    "users": Source("count(*)", "users u", "u.created_at", CLIENT),
    "paying": Source("count(DISTINCT p.user_id)", "payments p", "p.paid_at",
                     "p.status = 'paid' AND p.kind = 'tariff'"),
    "revenue": Source("coalesce(sum(p.amount), 0)", "payments p", "p.paid_at", "p.status = 'paid'"),
    "searches": Source("count(*)", "lot_research r JOIN users u ON u.id = r.user_id", "r.created_at", CLIENT),
    "active": Source("count(DISTINCT x.user_id)", "user_actions x JOIN users u ON u.id = x.user_id",
                     "x.at", CLIENT),
}

FEATURES = [
    # (ключ, подпись, источник: действие журнала или условие)
    ("research", "Поиск товаров с ИИ", "x.action = 'research' AND coalesce(x.details->>'more', 'false') <> 'true'"),
    ("research_more", "«Найти ещё»", "x.action = 'research' AND x.details->>'more' = 'true'"),
    ("spec_summary", "Выжимка техспецификации", "x.action = 'spec_summary'"),
    ("supplier_questions", "Что спросить у поставщика", "x.action = 'supplier_questions'"),
    ("export", "Выгрузка в Excel", "x.action = 'export'"),
    ("mark", "Избранное", "x.action = 'mark'"),
    ("note", "Заметки к лотам", "x.action = 'note'"),
    ("subscription_create", "Создал подписку", "x.action = 'subscription_create'"),
    ("telegram_link", "Подключил Telegram", "x.action = 'telegram_link'"),
    ("client_group_join", "Группа клиентов", "x.action = 'client_group_join'"),
]

PAYWALL_FEATURES = {
    "favorite": "Избранное", "spec_summary": "Выжимка техспецификации", "export": "Выгрузка в Excel",
    "subscription": "Подписка", "supplier_questions": "Что спросить у поставщика",
    "/my": "Мои лоты", "/research": "ИИ-подбор", "/subscriptions": "Подписки", "/matches": "Лента",
}
SOURCE_NAMES = {"direct": "Прямой заход", "internal": "Переходы внутри сайта", "google": "Google",
                "yandex": "Яндекс", "telegram": "Telegram", "whatsapp": "WhatsApp", "bing": "Bing",
                "mail.ru": "Mail.ru", "instagram": "Instagram", "facebook": "Facebook", "vk": "ВКонтакте",
                "youtube": "YouTube", "linkedin": "LinkedIn", "duckduckgo": "DuckDuckGo", "threads": "Threads"}
DEVICES = {"mobile": "Телефон", "tablet": "Планшет", "desktop": "Компьютер"}


def _rows(conn, sql: str, params: dict | None = None) -> list:
    return conn.execute(sql, params or {}).fetchall()


def _one(conn, sql: str, params: dict | None = None) -> Any:
    return conn.execute(sql, params or {}).fetchone()[0]


def _top(counter: Counter, n: int = 10) -> list[list]:
    return [[k, v] for k, v in counter.most_common(n)]


# --- Обзор ----------------------------------------------------------------------------


def overview(conn: psycopg.Connection, p: Period) -> dict[str, Any]:
    data = stats.pack(conn, SOURCES, p)
    data["new_visitors"] = _new_visitors(conn, p)
    data["heat"] = stats.heatmap(conn, Source("count(*)", "visits v", "v.at", HUMAN), p)
    data["client_hours"] = stats.hours(
        conn, Source("count(*)", "user_actions x JOIN users u ON u.id = x.user_id", "x.at", CLIENT), p)
    return data


def _new_visitors(conn, p: Period) -> list[int]:
    """[новые, вернувшиеся] за период: новый — метка создана в этом периоде."""
    start = stats.start_ts(p.start).timestamp()
    rows = _rows(conn, f"SELECT DISTINCT v.visitor FROM visits v WHERE {HUMAN} AND v.visitor IS NOT NULL "
                       "AND v.at >= %(from)s AND v.at < %(to)s", p.bounds)
    new = sum(1 for (vid,) in rows if (_created(vid) or 0) >= start)
    return [new, len(rows) - new]


def _created(visitor: str) -> int | None:
    try:
        return int(visitor.split(".", 1)[0], 16)
    except (ValueError, AttributeError):
        return None


# --- Посетители -----------------------------------------------------------------------


def visitors(conn: psycopg.Connection, p: Period) -> dict[str, Any]:
    def by(column: str, n: int = 12) -> list[list]:
        return [[k, c] for k, c in _rows(conn, f"""
            SELECT coalesce({column}, '—'), count(DISTINCT v.visitor) FROM visits v
            WHERE {HUMAN} AND v.at >= %(from)s AND v.at < %(to)s GROUP BY 1 ORDER BY 2 DESC LIMIT {n}""",
            p.bounds)]

    sources = [[SOURCE_NAMES.get(k, k.removeprefix("other:")), c] for k, c in by("v.source", 15)]
    countries = by("CASE v.country WHEN 'KZ' THEN 'Казахстан' WHEN 'other' THEN 'Другие страны' ELSE 'Не определено' END")
    landing = _rows(conn, f"""
        SELECT path, count(*) FROM (SELECT DISTINCT ON (v.visitor) v.path FROM visits v
            WHERE {HUMAN} AND v.visitor IS NOT NULL AND v.at >= %(from)s AND v.at < %(to)s
            ORDER BY v.visitor, v.at) first GROUP BY 1 ORDER BY 2 DESC LIMIT 12""", p.bounds)
    return {
        "sources": sources,
        "utm": by("v.utm", 10),
        "devices": [[DEVICES.get(k, k), c] for k, c in by("v.device")],
        "browsers": by("v.browser"),
        "countries": countries,
        "regions": by("v.region", 20),
        "cities": by("v.city", 15),
        "landing": [[_page_name(path), c] for path, c in landing],
        "new_returning": _new_visitors(conn, p),
        "bots": _one(conn, "SELECT count(*) FROM visits v WHERE v.is_bot AND v.at >= %(from)s AND v.at < %(to)s",
                     p.bounds),
    }


def _page_name(path: str) -> str:
    for prefix, name in (("/a/", "Объявление"), ("/lots/", "Лот"), ("/pay/", "Оплата")):
        if path.startswith(prefix):
            return name
    return {"/": "Главная (поиск объявлений)", "/lots": "Поиск лотов", "/announcements": "Поиск объявлений",
            "/analytics": "Анализ рынка", "/pricing": "Цены", "/register": "Регистрация", "/login": "Вход",
            "/forgot": "Восстановление пароля", "/my": "Мои лоты", "/research": "ИИ-подбор",
            "/subscriptions": "Подписки", "/matches": "Лента", "/settings": "Настройки",
            "/offer": "Оферта", "/privacy": "Политика конфиденциальности", "/contacts": "Контакты",
            "/dashboard": "Дашборд"}.get(path, path)


# --- Воронка --------------------------------------------------------------------------


def funnel(conn: psycopg.Connection, p: Period) -> dict[str, Any]:
    b = p.bounds
    visitors_n = _one(conn, f"SELECT count(DISTINCT v.visitor) FROM visits v WHERE {HUMAN} "
                            "AND v.at >= %(from)s AND v.at < %(to)s", b)
    register_page = _one(conn, f"SELECT count(DISTINCT v.visitor) FROM visits v WHERE {HUMAN} "
                               "AND v.path = '/register' AND v.at >= %(from)s AND v.at < %(to)s", b)
    sms, confirmed = _rows(conn, """SELECT count(*), count(*) FILTER (WHERE verified_at IS NOT NULL)
        FROM otp_requests WHERE purpose = 'register' AND created_at >= %(from)s AND created_at < %(to)s""", b)[0]
    # Дальше — по клиентам, зарегистрированным в периоде (когорта).
    cohort = f"{CLIENT} AND u.created_at >= %(from)s AND u.created_at < %(to)s"
    registered = _one(conn, f"SELECT count(*) FROM users u WHERE {cohort}", b)
    org = _one(conn, f"SELECT count(*) FROM users u WHERE {cohort} AND EXISTS "
                     "(SELECT 1 FROM client_orgs o WHERE o.user_id = u.id)", b)
    started = _one(conn, f"SELECT count(*) FROM users u WHERE {cohort} AND EXISTS "
                         "(SELECT 1 FROM payments p WHERE p.user_id = u.id)", b)
    paid = _one(conn, f"SELECT count(*) FROM users u WHERE {cohort} AND EXISTS "
                      "(SELECT 1 FROM payments p WHERE p.user_id = u.id AND p.status = 'paid' AND p.kind = 'tariff')", b)
    bought = _one(conn, f"SELECT count(*) FROM users u WHERE {cohort} AND EXISTS "
                        "(SELECT 1 FROM payments p WHERE p.user_id = u.id AND p.status = 'paid' AND p.kind = 'searches')", b)
    searched = _one(conn, f"SELECT count(*) FROM users u WHERE {cohort} AND EXISTS "
                          "(SELECT 1 FROM lot_research r WHERE r.user_id = u.id)", b)
    days_to_pay = _one(conn, f"""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d) FROM (
        SELECT extract(epoch FROM min(p.paid_at) - u.created_at) / 86400 AS d FROM users u
        JOIN payments p ON p.user_id = u.id AND p.status = 'paid' AND p.kind = 'tariff'
        WHERE {CLIENT} GROUP BY u.id, u.created_at) x""")
    paywall = Counter()
    paywall_visitors: dict[str, set] = {}
    for feature, visitor in _rows(conn, """SELECT props->>'feature', visitor FROM events
            WHERE name = 'paywall' AND at >= %(from)s AND at < %(to)s""", b):
        name = _paywall_name(feature or "")
        paywall[name] += 1
        paywall_visitors.setdefault(name, set()).add(visitor)
    # Кто после окна зарегистрировался: метка посетителя потом встречается с входом.
    converted = {name: _one(conn, "SELECT count(DISTINCT visitor) FROM visits WHERE user_id IS NOT NULL "
                                  "AND visitor = ANY(%s)", (list(v),)) for name, v in paywall_visitors.items()}
    abandoned = _rows(conn, """SELECT count(*) FILTER (WHERE status IN ('pending', 'cancelled')),
                                      count(*) FILTER (WHERE status = 'paid'), count(*)
        FROM payments WHERE created_at >= %(from)s AND created_at < %(to)s
          AND created_at < now() - interval '1 hour'""", b)[0]
    return {
        "steps": [["Посетители", visitors_n], ["Открыли регистрацию", register_page],
                  ["Получили SMS-код", sms], ["Подтвердили код", confirmed],
                  ["Зарегистрировались", registered], ["Указали организацию", org],
                  ["Начали оплату", started], ["Оплатили тариф", paid],
                  ["Купили поиски", bought], ["Запустили поиск", searched]],
        "paywall": [[name, count, len(paywall_visitors[name]), converted.get(name, 0)]
                    for name, count in paywall.most_common()],
        "payments": {"abandoned": abandoned[0], "paid": abandoned[1], "total": abandoned[2]},
        "days_to_pay": round(float(days_to_pay), 1) if days_to_pay is not None else None,
    }


def _paywall_name(feature: str) -> str:
    if feature in PAYWALL_FEATURES:
        return PAYWALL_FEATURES[feature]
    for key, name in PAYWALL_FEATURES.items():
        if key.startswith("/") and feature.startswith(key):
            return name
    return feature[:60] or "—"


# --- Функции --------------------------------------------------------------------------


def features(conn: psycopg.Connection, p: Period) -> dict[str, Any]:
    b = p.bounds
    paying = _one(conn, f"SELECT count(*) FROM users u WHERE {CLIENT} AND u.paid_until > now()") or 0
    rows = []
    for _key, label, condition in FEATURES:
        users_n, uses = _rows(conn, f"""SELECT count(DISTINCT x.user_id), count(*)
            FROM user_actions x JOIN users u ON u.id = x.user_id
            WHERE {CLIENT} AND {condition} AND x.at >= %(from)s AND x.at < %(to)s""", b)[0]
        rows.append([label, users_n, uses, round(users_n / paying * 100) if paying else None])
    ledger = dict(_rows(conn, """SELECT kind, coalesce(sum(abs(delta)), 0) FROM search_ledger
        WHERE created_at >= %(from)s AND created_at < %(to)s GROUP BY 1""", b))
    outstanding = _one(conn, f"""SELECT coalesce(sum(l.delta), 0) FROM search_ledger l
        JOIN users u ON u.id = l.user_id WHERE {CLIENT}""")
    cost = _rows(conn, """SELECT count(*), coalesce(sum((usage->>'cost_usd')::numeric), 0),
                                 avg(extract(epoch FROM finished_at - started_at)),
                                 count(*) FILTER (WHERE status = 'error')
        FROM lot_research WHERE created_at >= %(from)s AND created_at < %(to)s""", b)[0]
    summaries = _one(conn, "SELECT count(*) FROM spec_summaries WHERE created_at >= %(from)s AND created_at < %(to)s", b)
    questions = _one(conn, "SELECT count(*) FROM supplier_questions WHERE created_at >= %(from)s AND created_at < %(to)s", b)
    revenue_searches = _one(conn, """SELECT coalesce(sum(amount), 0) FROM payments WHERE status = 'paid'
        AND kind = 'searches' AND paid_at >= %(from)s AND paid_at < %(to)s""", b)
    return {
        "paying": paying,
        "features": rows,
        "searches": {"purchased": ledger.get("purchase", 0), "granted": ledger.get("grant", 0),
                     "spent": ledger.get("search", 0), "refunded": ledger.get("refund", 0),
                     "outstanding": outstanding, "outstanding_kzt": outstanding * SEARCH_PRICE},
        "ai": {"searches": cost[0], "cost_kzt": round(float(cost[1]) * USD_KZT),
               "avg_cost_kzt": round(float(cost[1]) * USD_KZT / cost[0]) if cost[0] else None,
               "avg_minutes": round(float(cost[2]) / 60, 1) if cost[2] else None, "errors": cost[3],
               "summaries": summaries, "questions": questions, "revenue_searches": revenue_searches},
    }


# --- Поиск на платформе ---------------------------------------------------------------


def search(conn: psycopg.Connection, p: Period) -> dict[str, Any]:
    regions, words, zero, methods, subjects, sums, scopes = (Counter() for _ in range(7))
    total = 0
    for (props,) in _rows(conn, "SELECT props FROM events WHERE name = 'search' AND at >= %(from)s AND at < %(to)s",
                          p.bounds):
        total += 1
        scopes["Лоты" if props.get("scope") == "lots" else "Объявления"] += 1
        for code in props.get("regions") or []:
            regions[REGIONS.get(code, code)] += 1
        for code in props.get("methods") or []:
            methods[METHODS.get(code, (code,))[0]] += 1
        for code in props.get("subjects") or []:
            subjects[SUBJECTS.get(int(code), str(code))] += 1
        q = " ".join((props.get("q") or "").lower().split())
        if q:
            words[q] += 1
            if not props.get("total"):
                zero[q] += 1
        sums[_sum_bucket(props.get("min_sum"), props.get("max_sum"))] += 1
    ai_regions = Counter(dict(_rows(conn, """SELECT region, count(*) FROM lot_research
        WHERE created_at >= %(from)s AND created_at < %(to)s GROUP BY 1""", p.bounds)))
    return {"total": total, "scopes": _top(scopes), "regions": _top(regions, 20), "words": _top(words, 20),
            "zero": _top(zero, 15), "methods": _top(methods), "subjects": _top(subjects),
            "sums": _top(sums), "ai_regions": _top(ai_regions, 15)}


def _sum_bucket(low: float | None, high: float | None) -> str:
    if not low and not high:
        return "Без ограничения"
    def fmt(v: float) -> str:
        return f"{v / 1e6:g} млн" if v >= 1e6 else f"{v / 1e3:g} тыс."
    return f"{fmt(low) if low else '0'} — {fmt(high) if high else '∞'}"


# --- Удержание ------------------------------------------------------------------------


def retention(conn: psycopg.Connection, p: Period, now: datetime) -> dict[str, Any]:
    def active(days: int) -> int:
        return _one(conn, f"""SELECT count(DISTINCT x.user_id) FROM user_actions x JOIN users u ON u.id = x.user_id
            WHERE {CLIENT} AND x.at > now() - make_interval(days => %(d)s)""", {"d": days})
    cohorts = []
    monday = now.date() - timedelta(days=now.date().weekday())
    for weeks_ago in range(8, 0, -1):
        start = monday - timedelta(weeks=weeks_ago)
        users = [row[0] for row in _rows(conn, f"""SELECT u.id FROM users u WHERE {CLIENT}
            AND u.created_at >= %(a)s AND u.created_at < %(b)s""",
            {"a": stats.start_ts(start), "b": stats.start_ts(start + timedelta(days=7))})]
        shares = []
        for week in range(1, 5):
            a, b = start + timedelta(weeks=week), start + timedelta(weeks=week + 1)
            if a > now.date():
                shares.append(None)
                continue
            back = _one(conn, """SELECT count(DISTINCT user_id) FROM user_actions WHERE user_id = ANY(%(u)s)
                AND at >= %(a)s AND at < %(b)s""", {"u": users, "a": stats.start_ts(a), "b": stats.start_ts(b)})
            shares.append(round(back / len(users) * 100) if users else None)
        cohorts.append([start.isoformat(), len(users), shares])
    renewals = _rows(conn, f"""SELECT count(*) FILTER (WHERE n >= 2), count(*) FILTER (WHERE n = 1)
        FROM (SELECT p.user_id, count(*) AS n FROM payments p JOIN users u ON u.id = p.user_id
              WHERE {CLIENT} AND p.status = 'paid' AND p.kind = 'tariff' GROUP BY 1) x""")[0]
    churned = _one(conn, f"""SELECT count(*) FROM users u WHERE {CLIENT} AND u.paid_until < now()
        AND u.paid_until >= %(from)s""", p.bounds)
    return {"dau": active(1), "wau": active(7), "mau": active(30), "cohorts": cohorts,
            "renewed": renewals[0], "once": renewals[1], "churned": churned,
            "at_risk": _at_risk(conn)}


def _at_risk(conn) -> list[list]:
    """Платят, но не заходят неделю, или тариф кончается в ближайшие 7 дней без активности."""
    return [[login, org, until.isoformat() if until else None, last.isoformat() if last else None, uid]
            for uid, login, org, until, last in _rows(conn, f"""
        SELECT u.id, u.email, o.name, u.paid_until,
               (SELECT max(at) FROM user_actions x WHERE x.user_id = u.id) AS last
        FROM users u LEFT JOIN client_orgs o ON o.user_id = u.id
        WHERE {CLIENT} AND u.paid_until > now()
          AND (coalesce((SELECT max(at) FROM user_actions x WHERE x.user_id = u.id), u.created_at)
               < now() - interval '7 days' OR u.paid_until < now() + interval '7 days')
        ORDER BY u.paid_until LIMIT 50""")]


# --- Деньги ---------------------------------------------------------------------------


def money(conn: psycopg.Connection, p: Period) -> dict[str, Any]:
    b = p.bounds
    revenue = {kind: stats.series(conn, Source("coalesce(sum(p.amount), 0)", "payments p", "p.paid_at",
                                               f"p.status = 'paid' AND p.kind = '{kind}'"), p)
               for kind in ("tariff", "searches")}
    count, total, refunds = _rows(conn, """SELECT count(*) FILTER (WHERE status = 'paid'),
        coalesce(sum(amount) FILTER (WHERE status = 'paid'), 0), count(*) FILTER (WHERE status = 'refunded')
        FROM payments WHERE coalesce(paid_at, created_at) >= %(from)s AND coalesce(paid_at, created_at) < %(to)s""", b)[0]
    payers = _one(conn, """SELECT count(DISTINCT user_id) FROM payments WHERE status = 'paid'
        AND paid_at >= %(from)s AND paid_at < %(to)s""", b)
    lifetime = _rows(conn, f"""SELECT count(DISTINCT p.user_id), coalesce(sum(p.amount), 0) FROM payments p
        JOIN users u ON u.id = p.user_id WHERE {CLIENT} AND p.status = 'paid'""")[0]
    paying = _one(conn, f"SELECT count(*) FROM users u WHERE {CLIENT} AND u.paid_until > now()")
    invoices = _one(conn, """SELECT count(*) FROM user_actions WHERE action = 'payment_invoice'
        AND at >= %(from)s AND at < %(to)s""", b)
    return {
        "labels": stats.labels(p), "revenue": revenue, "total": total, "count": count,
        "avg_check": round(total / count) if count else None,
        "arpu": round(total / payers) if payers else None,
        "ltv": round(lifetime[1] / lifetime[0]) if lifetime[0] else None,
        "mrr": paying * int(tariff.get(conn)["price"]), "paying": paying,
        "refunds": refunds, "kaspi_invoices": invoices,
    }


# --- Клиенты --------------------------------------------------------------------------


def clients(conn: psycopg.Connection) -> dict[str, Any]:
    rows = _rows(conn, f"""
        SELECT u.id, u.email, o.name, o.bin, u.created_at, u.paid_until,
               (SELECT max(at) FROM user_actions x WHERE x.user_id = u.id),
               (SELECT count(*) FROM lot_research r WHERE r.user_id = u.id),
               (SELECT coalesce(sum(delta), 0) FROM search_ledger l WHERE l.user_id = u.id),
               (SELECT coalesce(sum(amount), 0) FROM payments p WHERE p.user_id = u.id AND p.status = 'paid'),
               u.telegram_chat_id IS NOT NULL,
               (SELECT array_agg(DISTINCT x.action) FROM user_actions x WHERE x.user_id = u.id
                  AND x.action IN ('research', 'spec_summary', 'supplier_questions', 'export', 'mark',
                                   'subscription_create')),
               (SELECT v.region FROM visits v WHERE v.user_id = u.id AND v.region IS NOT NULL
                  ORDER BY v.at DESC LIMIT 1)
        FROM users u LEFT JOIN client_orgs o ON o.user_id = u.id
        WHERE {CLIENT} ORDER BY u.created_at DESC LIMIT 1000""")
    out = []
    for (uid, login, org, bin_, created, until, last, searches_n, balance, paid, tg, used, region) in rows:
        out.append({"id": uid, "login": login, "org": org, "bin": bin_,
                    "created": created.isoformat() if created else None,
                    "until": until.isoformat() if until else None,
                    "last": last.isoformat() if last else None, "searches": searches_n, "balance": balance,
                    "paid": paid, "telegram": tg,
                    "features": [ACTIONS.get(a, a) for a in (used or [])], "region": region})
    return {"clients": out}


# --- Состояние сервиса ----------------------------------------------------------------


def health(conn: psycopg.Connection, p: Period) -> dict[str, Any]:
    b = p.bounds
    speed = _rows(conn, f"""SELECT date_trunc('{p.unit}', v.at AT TIME ZONE '{stats.TZ}')::date,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY v.ms), percentile_cont(0.95) WITHIN GROUP (ORDER BY v.ms),
            count(*) FILTER (WHERE v.status >= 500)
        FROM visits v WHERE v.ms IS NOT NULL AND v.at >= %(from)s AND v.at < %(to)s GROUP BY 1""", b)
    by_day = {d: (p50, p95, errors) for d, p50, p95, errors in speed}
    labels = stats.buckets(p)
    slow = _rows(conn, """SELECT path, round(avg(ms)), count(*) FROM visits
        WHERE ms IS NOT NULL AND at >= %(from)s AND at < %(to)s GROUP BY 1 HAVING count(*) >= 3
        ORDER BY 2 DESC LIMIT 10""", b)
    ai = _rows(conn, """SELECT count(*), count(*) FILTER (WHERE status = 'error'),
            avg(extract(epoch FROM finished_at - started_at)) FILTER (WHERE status = 'done'),
            max(extract(epoch FROM finished_at - started_at))
        FROM lot_research WHERE created_at >= %(from)s AND created_at < %(to)s""", b)[0]
    incidents = _rows(conn, """SELECT id, stage, error_type, count, status, last_at FROM incidents
        WHERE last_at >= %(from)s ORDER BY id DESC LIMIT 20""", b)
    last_sync = _one(conn, "SELECT max(updated_at) FROM sync_state WHERE entity IN ('announcements', 'lots')")
    return {
        "labels": [d.isoformat() for d in labels],
        "p50": [round(float(by_day.get(d, (0, 0, 0))[0] or 0)) for d in labels],
        "p95": [round(float(by_day.get(d, (0, 0, 0))[1] or 0)) for d in labels],
        "errors": [int(by_day.get(d, (0, 0, 0))[2] or 0) for d in labels],
        "slow": [[_page_name(path), int(ms), n] for path, ms, n in slow],
        "ai": {"total": ai[0], "errors": ai[1], "avg_minutes": round(float(ai[2]) / 60, 1) if ai[2] else None,
               "max_minutes": round(float(ai[3]) / 60, 1) if ai[3] else None},
        "incidents": [[i, s, e, c, st, at.isoformat()] for i, s, e, c, st, at in incidents],
        "last_sync": last_sync.isoformat() if last_sync else None,
    }


SECTIONS = ("overview", "visitors", "funnel", "features", "search", "retention", "money", "clients", "health")


def section(conn: psycopg.Connection, name: str, days: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    p = stats.period(days, now)
    head = {"days": p.days, "unit": p.unit, "start": p.start.isoformat(), "end": p.end.isoformat()}
    if name == "overview":
        return {**head, **overview(conn, p)}
    if name == "visitors":
        return {**head, **visitors(conn, p)}
    if name == "funnel":
        return {**head, **funnel(conn, p)}
    if name == "features":
        return {**head, **features(conn, p)}
    if name == "search":
        return {**head, **search(conn, p)}
    if name == "retention":
        return {**head, **retention(conn, p, now)}
    if name == "money":
        return {**head, **money(conn, p)}
    if name == "clients":
        return {**head, **clients(conn)}
    if name == "health":
        return {**head, **health(conn, p)}
    raise ValueError(name)
