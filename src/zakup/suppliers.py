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
    cursor = conn.execute(
        f"""SELECT s.bin, coalesce(d.name_ru, s.bin) AS name_ru, d.name_kz, d.form,
                   sum(s.wins) FILTER (WHERE s.region = %(region)s) AS here_wins,
                   sum(s.offers) FILTER (WHERE s.region = %(region)s) AS here_offers,
                   sum(s.wins) AS wins, sum(s.offers) AS offers,
                   avg(s.discount) AS discount, max(s.last_at) AS last_at,
                   array_agg(DISTINCT s.region) FILTER (WHERE s.region <> '') AS regions
            FROM supplier_activity s LEFT JOIN org_directory d ON d.bin = s.bin
            WHERE s.code {'=' if exact else 'LIKE'} %(pattern)s
            GROUP BY s.bin, d.name_ru, d.name_kz, d.form""",
        {"pattern": pattern, "region": region})
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def for_lot(conn: psycopg.Connection, lot_id: int, level: str | None = None) -> dict[str, Any] | None:
    """Участники по уровням; None — у лота нет кода или никого не нашлось."""
    place = lot_place(conn, lot_id)
    if not place or len(place[0]) < 2:
        return None
    code, region = place
    available: dict[str, int] = {}
    chosen, rows = None, []
    for item in LEVELS:
        found = _found(conn, _prefix(code, item), item == "code", region)
        available[item] = len(found)
        if (level == item) or (level not in LEVELS and chosen is None and len(found) >= MIN_FOUND):
            chosen, rows = item, found
    if chosen is None:
        chosen = next((item for item in reversed(LEVELS) if available[item]), None)
        if chosen is None:
            return None
        rows = _found(conn, _prefix(code, chosen), chosen == "code", region)
    here = sorted((r for r in rows if region and r["here_offers"]),
                  key=lambda r: (-(r["here_wins"] or 0), -(r["here_offers"] or 0), -(r["wins"] or 0)))
    rest = sorted((r for r in rows if not (region and r["here_offers"])),
                  key=lambda r: (-(r["wins"] or 0), -(r["offers"] or 0), r["last_at"] is None,
                                 -(r["last_at"].timestamp() if r["last_at"] else 0)))
    return {"code": code, "region": region, "level": chosen, "available": available,
            "class_name": catalog.division_name(code[:2]), "here": here[:LIMIT], "rest": rest[:LIMIT],
            "here_total": len(here), "rest_total": len(rest)}
