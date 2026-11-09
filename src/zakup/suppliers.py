"""Блок «Конкуренты и поставщики» на странице лота: кто уже участвовал и выигрывал
в закупках такого же товара — сначала в регионе поставки лота, потом по Казахстану.

Такое же — по коду ЕНС ТРУ лота, от точного к общему: тот же код (диваны), тот же класс
КПВЭД — 6 цифр (мебель для сидения), тот же раздел — 2 цифры (вся мебель). Берётся самый
точный уровень, где нашлось хотя бы MIN_FOUND участников; остальные — переключателем.
Данные — supplier_activity (пересчёт вместе со справочником организаций).
"""

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


def for_lot(conn: psycopg.Connection, lot_id: int, level: str | None = None,
            paid: bool = True) -> dict[str, Any] | None:
    """Участники по уровням; None — у лота нет кода или никого не нашлось. Без тарифа —
    по SHOWN в группе и без снижения цены (отрезается здесь, а не скрывается в вёрстке)."""
    place = lot_place(conn, lot_id)
    if not place or len(place[0]) < 2:
        return None
    code, region = place
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
    return {"code": code, "region": region, "level": level, "available": available,
            "class_name": catalog.division_name(code[:2]), "here": here[:limit], "rest": rest[:limit],
            "here_total": len(here), "rest_total": len(rest)}
