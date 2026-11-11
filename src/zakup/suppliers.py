"""Блок «Конкуренты и поставщики» на странице лота: кто уже участвовал и выигрывал
в закупках такого же товара — сначала в регионе поставки лота, потом по Казахстану.

Такое же — по коду ЕНС ТРУ лота, от точного к общему: тот же код (диваны), тот же класс
КПВЭД — 6 цифр (мебель для сидения), тот же раздел — 2 цифры (вся мебель). Берётся самый
точный уровень, где нашлось хотя бы MIN_FOUND участников; остальные — переключателем.
Данные — supplier_activity (пересчёт вместе со справочником организаций).

У половины лотов портал не отдаёт код ЕНС ТРУ (пустые «Планы» в API) — тогда уровень
«по названию»: участники итогов прошлых лотов с теми же словами в названии.
"""

import time
from typing import Any

import psycopg

from zakup import catalog

LEVELS = ("code", "class", "division")
MIN_FOUND = 3
SHOWN = 5  # без тарифа — по столько в каждой группе
LIMIT = 30


def _prefix(code: str, level: str) -> str:
    if level == "code":
        return code
    return code[:6] + "%" if level == "class" else code[:2] + "%"


def lot_place(conn: psycopg.Connection, lot_id: int) -> tuple[str, str] | None:
    """(код ЕНС ТРУ, регион поставки) лота; None — лота нет."""
    row = conn.execute(
        """SELECT coalesce(l.enstru[1], ''), coalesce(left((coalesce(nullif(l.kato, '{}'), a.kato))[1], 2), '')
           FROM lots l LEFT JOIN announcements a ON a.id = l.trd_buy_id WHERE l.id = %s""", (lot_id,)).fetchone()
    return (row[0], row[1]) if row else None


def _found(conn: psycopg.Connection, pattern: str, exact: bool, region: str) -> list[dict[str, Any]]:
    # Сначала свести по участнику, потом подцепить названия — не соединять весь справочник.
    cursor = conn.execute(
        f"""SELECT s.bin, coalesce(d.name_ru, s.bin) AS name_ru, d.name_kz, d.form, s.here_wins, s.here_offers,
                   s.wins, s.offers, s.discount, s.last_at, s.regions
            FROM (SELECT bin,
                         sum(wins) FILTER (WHERE region = %(region)s) AS here_wins,
                         sum(offers) FILTER (WHERE region = %(region)s) AS here_offers,
                         sum(wins) AS wins, sum(offers) AS offers,
                         sum(discount_sum) / nullif(sum(discount_n), 0) AS discount, max(last_at) AS last_at,
                         array_agg(DISTINCT region) FILTER (WHERE region <> '') AS regions
                  FROM supplier_activity WHERE code {'=' if exact else 'LIKE'} %(pattern)s GROUP BY bin) s
            LEFT JOIN org_directory d ON d.bin = s.bin""",
        {"pattern": pattern, "region": region})
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _available(conn: psycopg.Connection, code: str) -> dict[str, int]:
    """Сколько участников на каждом уровне — одним запросом по разделу."""
    exact, klass, division = conn.execute(
        """SELECT count(DISTINCT bin) FILTER (WHERE code = %(code)s),
                  count(DISTINCT bin) FILTER (WHERE code LIKE %(class)s), count(DISTINCT bin)
           FROM supplier_activity WHERE code LIKE %(division)s""",
        {"code": code, "class": _prefix(code, "class"), "division": _prefix(code, "division")}).fetchone()
    return {"code": exact, "class": klass, "division": division}


# Сколько прошлых лотов с итогами брать для подбора по названию: самые свежие.
NAME_LOTS = 2000


NAME_TTL = 1800
NAME_CACHE = 5000
_name_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


def _by_name(conn: psycopg.Connection, lot_id: int, region: str) -> list[dict[str, Any]]:
    """Подбор по названию держим 30 минут: считается по тысячам итогов (~0,4 с), а меняется
    медленно. Ключ — название и регион: у тысяч лотов одинаковые названия («Диван»), так
    повторный расчёт редок. Копии строк — вызывающий режет снижение цены для гостей."""
    name = conn.execute("SELECT lower(btrim(name_ru)) FROM lots WHERE id = %s", (lot_id,)).fetchone()
    key = ((name or [""])[0] or "", region)
    hit = _name_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return [dict(row) for row in hit[1]]
    rows = _by_name_query(conn, lot_id, region)
    if len(_name_cache) >= NAME_CACHE:  # самые старые — вон (словарь помнит порядок вставки)
        for old_key in list(_name_cache)[:NAME_CACHE // 5]:
            del _name_cache[old_key]
    _name_cache[key] = (time.monotonic() + NAME_TTL, rows)
    return [dict(row) for row in rows]


def _by_name_query(conn: psycopg.Connection, lot_id: int, region: str) -> list[dict[str, Any]]:
    """Участники итогов прошлых лотов, в названии которых те же слова, что у этого лота."""
    name = conn.execute("SELECT name_ru FROM lots WHERE id = %s", (lot_id,)).fetchone()
    if not name or not (name[0] or "").strip():
        return []
    cursor = conn.execute(
        f"""WITH m AS (
                SELECT l.id, coalesce(left((coalesce(nullif(l.kato, '{{}}'), a.kato))[1], 2), '') AS region,
                       a.end_date, l.amount
                FROM lots l JOIN lot_results r ON r.lot_id = l.id
                LEFT JOIN announcements a ON a.id = l.trd_buy_id
                WHERE l.id <> %(lot)s AND l.tsv @@ plainto_tsquery('russian', %(name)s)
                ORDER BY r.result_date DESC NULLS LAST LIMIT {NAME_LOTS}),
            o AS (
                SELECT o->>'bin' AS bin, m.region, m.end_date,
                       (r.outcome = 'winner' AND r.winner_bin = o->>'bin') AS won,
                       CASE WHEN r.outcome = 'winner' AND r.winner_bin = o->>'bin' AND m.amount > 0
                                 AND r.winner_total BETWEEN m.amount * 0.05 AND m.amount
                            THEN 1 - r.winner_total / m.amount END AS discount
                FROM m JOIN lot_results r ON r.lot_id = m.id, jsonb_array_elements(r.offers) o
                WHERE o->>'bin' ~ '^[0-9]{{12}}$')
            SELECT s.bin, coalesce(d.name_ru, s.name, s.bin) AS name_ru, d.name_kz, d.form, s.here_wins,
                   s.here_offers, s.wins, s.offers, s.discount, s.last_at, s.regions
            FROM (SELECT bin, NULL::text AS name,
                         count(*) FILTER (WHERE won AND region = %(region)s) AS here_wins,
                         count(*) FILTER (WHERE region = %(region)s) AS here_offers,
                         count(*) FILTER (WHERE won) AS wins, count(*) AS offers, avg(discount) AS discount,
                         max(end_date) AS last_at,
                         array_agg(DISTINCT region) FILTER (WHERE region <> '') AS regions
                  FROM o GROUP BY bin) s
            LEFT JOIN org_directory d ON d.bin = s.bin""",
        {"lot": lot_id, "name": name[0], "region": region})
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def same_product(level: str) -> str:
    """«Такой же товар» для лотов l (параметры code, name): тот же код ЕНС ТРУ или, у лотов
    без кода, те же слова в названии. Общее для этого блока и zakup.lot_insights."""
    if level == "code":
        return "left(l.enstru[1], 2) = left(%(code)s, 2) AND l.enstru[1] = %(code)s"
    return "l.tsv @@ plainto_tsquery('russian', %(name)s)"


HISTORY_ROWS = 20  # строк истории на участника
HISTORY_TTL = 1800
_history_cache: dict[tuple, tuple[float, dict[str, dict[str, Any]]]] = {}


def offer_history(conn: psycopg.Connection, lot_id: int, level: str, code: str,
                  bins: list[str]) -> dict[str, dict[str, Any]]:
    """Что предлагали эти участники в таких же лотах (тот же код ЕНС ТРУ или, без кода, те же
    слова в названии) — по последним NAME_LOTS лотам с итогами и только в той же единице
    измерения, что у этого лота (штуки со штуками, а не с комплектами). Цена за единицу —
    сумма заявки / количество лота; если участник подавал заявку несколько раз — самая низкая.
    Держим 30 минут, как подбор по названию: считается по тысячам итогов."""
    if not bins or level not in ("code", "name"):
        return {}
    lot = conn.execute("SELECT coalesce(name_ru, ''), coalesce(lower(unit_name), '') FROM lots WHERE id = %s",
                       (lot_id,)).fetchone()
    if not lot:
        return {}
    name, unit = lot
    key = (level, code if level == "code" else name.lower().strip(), unit, tuple(sorted(bins)))
    hit = _history_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    match = same_product(level)
    cursor = conn.execute(
        f"""WITH m AS (
                SELECT l.id, l.name_ru, l.name_kz, l.count, l.amount, l.unit_name, l.unit_name_kz,
                       r.result_date, r.outcome, r.winner_bin, r.offers
                FROM lots l JOIN lot_results r ON r.lot_id = l.id
                WHERE {match} AND l.id <> %(lot)s AND coalesce(lower(l.unit_name), '') = %(unit)s
                ORDER BY r.result_date DESC NULLS LAST LIMIT {NAME_LOTS})
            SELECT DISTINCT ON (o->>'bin', m.id) o->>'bin', m.id, m.name_ru, m.name_kz, m.count, m.amount,
                   m.unit_name, m.unit_name_kz, m.result_date, m.outcome, m.winner_bin,
                   nullif(o->>'total', '')::numeric AS total
            FROM m, jsonb_array_elements(m.offers) o
            WHERE o->>'bin' = ANY(%(bins)s) AND nullif(o->>'total', '') IS NOT NULL
            ORDER BY o->>'bin', m.id, total""",
        {"code": code, "name": name, "lot": lot_id, "unit": unit, "bins": bins})
    result: dict[str, dict[str, Any]] = {}
    for bin_, lot_, name_ru, name_kz, count, amount, unit_ru, unit_kz, date, outcome, winner, total in cursor:
        entry = result.setdefault(bin_, {"rows": []})
        entry["rows"].append({
            "lot": lot_, "name_ru": name_ru, "name_kz": name_kz, "count": float(count or 0),
            "unit_ru": unit_ru, "unit_kz": unit_kz, "date": date, "total": float(total),
            "unit": float(total) / float(count) if count else None,
            "plan_unit": float(amount) / float(count) if count and amount else None,
            "result": "won" if outcome == "winner" and winner == bin_ else "lost" if outcome == "winner" else "failed"})
    for entry in result.values():
        units = [row["unit"] for row in entry["rows"] if row["unit"]]
        entry["min"], entry["max"] = (min(units), max(units)) if units else (None, None)
        entry["total"] = len(entry["rows"])
        entry["won"] = sum(1 for row in entry["rows"] if row["result"] == "won")
        entry["rows"].sort(key=lambda row: row["date"].timestamp() if row["date"] else 0, reverse=True)
        entry["rows"] = entry["rows"][:HISTORY_ROWS]
    if len(_history_cache) >= NAME_CACHE:
        for old_key in list(_history_cache)[:NAME_CACHE // 5]:
            del _history_cache[old_key]
    _history_cache[key] = (time.monotonic() + HISTORY_TTL, result)
    return result


def for_lot(conn: psycopg.Connection, lot_id: int, level: str | None = None,
            paid: bool = True) -> dict[str, Any] | None:
    """Участники по уровням; None — никого не нашлось. Без тарифа — по SHOWN в группе и
    без снижения цены (отрезается здесь, а не скрывается в вёрстке)."""
    place = lot_place(conn, lot_id)
    if not place:
        return None
    code, region = place
    if len(code) < 2:
        rows = _by_name(conn, lot_id, region)
        if not rows:
            return None
        level, available = "name", {"name": len(rows)}
    else:
        available = _available(conn, code)
        if level not in LEVELS:
            level = next((item for item in LEVELS if available[item] >= MIN_FOUND), None) or next(
                (item for item in reversed(LEVELS) if available[item]), None)
            if level is None:
                return None
        rows = _found(conn, _prefix(code, level), level == "code", region) if available[level] else []
    here = sorted((r for r in rows if region and r["here_offers"]),
                  key=lambda r: (-(r["here_wins"] or 0), -(r["here_offers"] or 0), -(r["wins"] or 0)))
    rest = sorted((r for r in rows if not (region and r["here_offers"])),
                  key=lambda r: (-(r["wins"] or 0), -(r["offers"] or 0), r["last_at"] is None,
                                 -(r["last_at"].timestamp() if r["last_at"] else 0)))
    limit = LIMIT if paid else SHOWN
    if not paid:
        for row in here[:limit] + rest[:limit]:
            row["discount"] = None
    else:
        # Цены их заявок в таких же лотах — платная часть, как все участники итогов с ценами.
        shown_rows = here[:limit] + rest[:limit]
        history = offer_history(conn, lot_id, level, code, [r["bin"] for r in shown_rows])
        for row in shown_rows:
            row["history"] = history.get(row["bin"])
    return {"code": code, "region": region, "level": level, "available": available,
            "class_name": catalog.division_name(code[:2]) if code else None, "here": here[:limit], "rest": rest[:limit],
            "here_total": len(here), "rest_total": len(rest)}
