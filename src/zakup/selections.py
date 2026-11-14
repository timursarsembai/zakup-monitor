"""Подборки тендеров — открытые страницы для поисковиков (ROADMAP, «Идеи», п. 14):
горящие (приём заявок сегодня или завтра), крупные, запрос ценовых предложений, для малого
бизнеса — по всей стране и по регионам (/tendery/<подборка>, /tendery/<подборка>/<регион>).

Подборка — фильтр поиска (Filters.public() и одно условие сверху), ссылка «все в поиске» —
с тем же условием. Регион страницы, как в catalog, — по первому месту поставки, а в поиске
лот попадает по любому своему месту: там лотов бывает чуть больше.

Цифры по регионам — как catalog.counts: в памяти и снимком в базе, пересчёт — в фоне
(вместе с каталогом), посетитель его не ждёт. Списки лотов — в памяти PAGE_TTL.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from zakup.catalog import LOT_REGION, SNAPSHOT_MAX_AGE
from zakup.filters import PORTAL_TZ, REGIONS, Filters

logger = logging.getLogger(__name__)

LIST_LIMIT = 50
TTL = 3600
PAGE_TTL = 900
HOT_DAYS = 1   # сегодня и завтра (за 3 дня — больше двух третей открытых лотов)
LARGE_FROM = Decimal(50_000_000)
SMALL_TO = Decimal(3_000_000)
SNAPSHOT_KEY = "selection_counts"


@dataclass(frozen=True)
class Selection:
    slug: str
    title: str        # ru; перевод — i18n_kk (через _ в шаблоне)
    lead: str         # одна фраза под заголовком и в description


SELECTIONS: dict[str, Selection] = {s.slug: s for s in (
    Selection("goryashchie", "Горящие тендеры",
              "Открытые лоты, приём заявок по которым заканчивается сегодня или завтра."),
    Selection("krupnye", "Крупные тендеры",
              "Открытые лоты с плановой суммой от 50 млн ₸ — сначала самые крупные."),
    Selection("zapros-cen", "Запрос ценовых предложений",
              "Открытые лоты, закупаемые способом запроса ценовых предложений — самым быстрым и простым."),
    Selection("malyy-biznes", "Тендеры для малого бизнеса",
              "Открытые лоты с плановой суммой до 3 млн ₸: небольшие поставки, где проще начать."),
)}


def _today(now: datetime) -> date:
    return now.astimezone(PORTAL_TZ).date()


def filters(slug: str, region: str | None = None, now: datetime | None = None) -> Filters:
    """Фильтр поиска для подборки (и для ссылки «все в поиске»)."""
    now = now or datetime.now(UTC)
    base = replace(Filters.public(), regions=[region] if region else [])
    if slug == "goryashchie":
        return replace(base, deadline=(_today(now) + timedelta(days=HOT_DAYS)).isoformat(), sort="end")
    if slug == "krupnye":
        return replace(base, min_sum=LARGE_FROM, sort="amount_desc")
    if slug == "zapros-cen":
        return replace(base, methods=["zcp"], sort="end")
    if slug == "malyy-biznes":
        return replace(base, max_sum=SMALL_TO, sort="end")
    raise KeyError(slug)


Counts = dict[str, dict[str, tuple[int, float]]]   # подборка → регион ("" — без места) → (число, сумма)
_counts: dict[str, Any] = {"at": 0.0, "rows": {}}
_pages: dict[tuple, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()
_ORDER = {"end": "a.end_date, l.id", "amount_desc": "l.amount DESC NULLS LAST, a.end_date, l.id"}


def refresh(conn: psycopg.Connection, now: datetime | None = None) -> Counts:
    """Пересчитать цифры всех подборок, положить в память и в снимок."""
    now = now or datetime.now(UTC)
    result: Counts = {}
    for slug in SELECTIONS:
        where, params = filters(slug, now=now).where(now, "lots")
        rows = conn.execute(
            f"""SELECT coalesce({LOT_REGION}, ''), count(*), coalesce(sum(l.amount), 0)
                FROM lots l, announcements a WHERE {where} GROUP BY 1""", params).fetchall()
        result[slug] = {r: (n, float(s)) for r, n, s in rows}
    with _lock:
        _counts.update(rows=result, at=time.monotonic() + TTL)
    try:
        with conn.transaction():
            conn.execute("""INSERT INTO app_settings (key, value) VALUES (%s, %s)
                            ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                         (SNAPSHOT_KEY, json.dumps({"at": datetime.now(UTC).isoformat(), "rows": result})))
    except psycopg.Error:
        logger.exception("selections: snapshot not saved")
    return result


def _load_snapshot(conn: psycopg.Connection) -> Counts:
    try:
        with conn.transaction():
            row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (SNAPSHOT_KEY,)).fetchone()
        if not row:
            return {}
        stored = json.loads(row[0])
        if datetime.now(UTC) - datetime.fromisoformat(stored["at"]) > SNAPSHOT_MAX_AGE:
            return {}
        return {slug: {r: (int(n), float(s)) for r, (n, s) in by_region.items()}
                for slug, by_region in stored["rows"].items()}
    except (psycopg.Error, ValueError, TypeError, KeyError):
        logger.exception("selections: snapshot not loaded")
        return {}


def counts(conn: psycopg.Connection, background: Callable[[], None] | None = None) -> Counts:
    """Цифры подборок. С background (сайт) посетитель пересчёта не ждёт: прежние цифры — из
    памяти или снимка, пересчёт — background(); один за раз, неудача — повтор через минуту.
    Без прежних цифр (самый первый раз) или без background — считает сразу."""
    with _lock:
        rows, fresh = _counts["rows"], _counts["at"] > time.monotonic()
    if rows and fresh:
        return rows
    if not rows and background is not None:
        rows = _load_snapshot(conn)
        if rows:
            with _lock:
                if not _counts["rows"]:
                    _counts["rows"] = rows
                rows = _counts["rows"]
    with _lock:
        take = _counts["at"] <= time.monotonic()
        if take:
            _counts["at"] = time.monotonic() + 60
    if rows and (background is not None or not take):
        if take and background is not None:
            background()
        return rows
    return refresh(conn)


def _total(by_region: dict[str, tuple[int, float]]) -> tuple[int, float]:
    return sum(n for n, _ in by_region.values()), sum(s for _, s in by_region.values())


def page(conn: psycopg.Connection, slug: str, region: str | None = None,
         background: Callable[[], None] | None = None) -> dict[str, Any]:
    """Число и сумма открытых лотов подборки (в регионе — по первому месту поставки), первые
    LIST_LIMIT лотов и соседние регионы."""
    by_region = counts(conn, background).get(slug, {})
    now = datetime.now(UTC)
    key = (slug, region, _today(now))   # «горящие» — до завтрашнего дня: со сменой даты — заново
    with _lock:
        cached = _pages.get(key)
    if cached and cached[0] > time.monotonic():
        data = cached[1]
    else:
        search = filters(slug, region, now)
        where, params = replace(search, regions=[]).where(now, "lots")
        if region:
            where += f" AND {LOT_REGION} = %(page_region)s"
            params = {**params, "page_region": region}
        lots = conn.execute(
            f"""SELECT l.id, l.name_ru, l.name_kz, l.amount, l.customer_bin,
                       coalesce(a.customer_name_ru, l.customer_name_ru), a.end_date, l.lot_number
                FROM lots l, announcements a WHERE {where}
                ORDER BY {_ORDER[search.sort]} LIMIT {LIST_LIMIT}""", params).fetchall()
        data = {"lots": lots, "search": search.to_query("lots")}
        with _lock:
            if len(_pages) > 500:   # подборок × регионов около сотни
                _pages.clear()
            _pages[key] = (time.monotonic() + PAGE_TTL, data)
    # Приём заявок закончился, пока список в памяти, — лот уже не открыт.
    lots = [lot for lot in data["lots"] if lot[6] is None or lot[6] > now]
    total, amount = by_region.get(region, (0, 0.0)) if region else _total(by_region)
    nearby = sorted(((r, n) for r, (n, _) in by_region.items() if r in REGIONS and n), key=lambda i: -i[1])
    return {"lots": lots, "search": data["search"], "total": max(total, len(lots)), "amount": amount,
            "nearby": nearby}


def overview(conn: psycopg.Connection, background: Callable[[], None] | None = None,
             region: str | None = None) -> list[tuple[Selection, int, float]]:
    """Подборки с числом и суммой открытых лотов (по стране или в регионе)."""
    rows = counts(conn, background)
    out = []
    for selection in SELECTIONS.values():
        by_region = rows.get(selection.slug, {})
        out.append((selection, *(by_region.get(region, (0, 0.0)) if region else _total(by_region))))
    return out


def sitemap_paths(conn: psycopg.Connection, background: Callable[[], None] | None = None) -> list[str]:
    """Обзор, подборки и подборки в регионах, где есть открытые лоты."""
    rows = counts(conn, background)
    paths = ["/tendery"]
    for slug in SELECTIONS:
        paths.append(f"/tendery/{slug}")
        paths += [f"/tendery/{slug}/{r}" for r, (n, _) in sorted(rows.get(slug, {}).items()) if n and r in REGIONS]
    return paths
