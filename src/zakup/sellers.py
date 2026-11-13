"""Продавцы каталога товаров (docs/CATALOG_PLAN.md, этап 2).

Найденные поиском с ИИ (kind = 'found'): продавец — как у базы контактов (zakup.contacts):
на маркетплейсе — продавец на нём (domain + seller_key), у прочих сайтов — весь сайт
(seller_key = ''). Свои магазины пользователей (kind = 'own') — этап 3.

Товары каталога ссылаются на продавца (product_catalog.seller_id): link() проставляет её
новым находкам (products.remember) и всем прежним без неё (`zakup tidy`, каждые 15 минут).
Страница продавца — /sellers/<номер>-<название латиницей>, адрес держится на номере.
"""

import re
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from zakup import contacts, products

PAGE_SIZE = 30
# Не продавец, а сборная строка ИИ: у таких своей страницы нет.
_GENERIC = re.compile(r"несколько продавцов|частн\w* объявлени|разные продавцы|продавец не указан", re.I)
_TAIL = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
SORTS = {"products": "Больше товаров", "name": "По названию", "new": "Сначала новые"}
_ORDER = {"products": "products DESC, s.id DESC", "name": "s.name, s.id", "new": "s.created_at DESC, s.id DESC"}


def _marketplace(domain: str) -> str | None:
    """Маркетплейс домена (almaty.satu.kz → satu.kz) или None."""
    if domain in contacts.MARKETPLACES:
        return domain
    return next((m for m in contacts.MARKETPLACES if domain.endswith("." + m)), None)


def clean_name(seller: str | None, domain: str) -> str:
    """«Дом Диванов (almaty.domdivanov.kz)», «TOMILI (Satu.kz)» → без хвоста про сам сайт."""
    name = " ".join((seller or "").split())
    base = domain.split(".")[-2] if domain.count(".") >= 1 else domain
    match = _TAIL.match(name)
    if match and (base in match.group(2).lower() or domain in match.group(2).lower()):
        name = match.group(1).strip()
    return name[:150]


def identity(url: str, seller: str | None) -> tuple[str, str, str] | None:
    """(domain, seller_key, название) продавца товара или None — продавца не определить."""
    domain = contacts._domain(url)
    if not domain:
        return None
    name = clean_name(seller, domain)
    if name and _GENERIC.search(name):
        return None
    if market := _marketplace(domain):
        # Один продавец на региональных поддоменах — одна страница.
        domain = market
        key = contacts.seller_key(name)
        # Без имени продавца или с именем самого маркетплейса — не знаем, кто продаёт.
        if not key or key.split(".")[0] == domain.split(".")[-2]:
            return None
        return domain, key, name
    return domain, "", name or domain


def link(conn: psycopg.Connection, keys: list[str] | None = None) -> int:
    """Проставляет продавца товарам без него (keys — только эти url_key). Транзакцию
    коммитит вызывающий.

    Блокировки — в одном порядке у всех: сначала товары, потом продавцы по порядку.
    Товары, занятые встречным поиском, пропускаем (SKIP LOCKED) — их привяжет он сам или
    следующий проход; существующих продавцов не обновляем (DO NOTHING) — не блокируем."""
    rows = conn.execute(
        "SELECT id, url, seller FROM product_catalog WHERE seller_id IS NULL"
        + (" AND url_key = ANY(%s)" if keys is not None else "") + " ORDER BY id FOR UPDATE SKIP LOCKED",
        (keys,) if keys is not None else ()).fetchall()
    groups: dict[tuple[str, str, str], list[int]] = {}
    for pid, url, seller in rows:
        if ident := identity(url, seller):
            groups.setdefault(ident, []).append(pid)
    linked = 0
    for (domain, key, name), ids in sorted(groups.items()):
        row = conn.execute(
            """INSERT INTO sellers (kind, domain, seller_key, name, website)
               VALUES ('found', %s, %s, %s, %s) ON CONFLICT (domain, seller_key) DO NOTHING RETURNING id""",
            (domain, key, name, None if _marketplace(domain) else f"https://{domain}")).fetchone()
        seller_id = row[0] if row else conn.execute(
            "SELECT id FROM sellers WHERE domain = %s AND seller_key = %s", (domain, key)).fetchone()[0]
        linked += conn.execute("UPDATE product_catalog SET seller_id = %s WHERE id = ANY(%s)",
                               (seller_id, ids)).rowcount
    return linked


def path(row: dict[str, Any]) -> str:
    return f"/sellers/{row['id']}-{products.slug(row['name'])}"


def _phones(conn: psycopg.Connection, seller: dict[str, Any]) -> list[dict]:
    """Телефоны: свои у продавца (этап 3) или собранные поиском (seller_contacts)."""
    if seller["phones"]:
        return seller["phones"]
    if seller["kind"] != "found":
        return []
    row = conn.execute("SELECT phones FROM seller_contacts WHERE domain = %s AND seller = %s",
                       (seller["domain"], seller["seller_key"])).fetchone()
    return [p for p in (row[0] if row else []) if p.get("number")][:contacts.MAX_PHONES]


def phone_list(phones: list[dict]) -> list[dict[str, Any]]:
    """Телефоны для показа: номер (+7 (701) 111-22-33), tel — для ссылки (только у
    распознанного казахстанского номера, иначе None — номер просто текстом), whatsapp."""
    out = []
    for ph in phones or []:
        if not ph.get("number"):
            continue
        normal = contacts.normalize(ph["number"])
        out.append({"number": normal or ph["number"], "label": ph.get("label") or "",
                    "tel": "+" + re.sub(r"\D", "", normal) if normal else None,
                    "whatsapp": bool(ph.get("whatsapp"))})
    return out


def get(conn: psycopg.Connection, seller_id: int) -> dict[str, Any] | None:
    cur = conn.cursor(row_factory=dict_row)
    seller = cur.execute(
        """SELECT s.*, (SELECT mode() WITHIN GROUP (ORDER BY p.city) FROM product_catalog p
                        WHERE p.seller_id = s.id AND p.city IS NOT NULL) AS city_guess,
                  -- в счёте — что видно в каталоге (снятые с продажи — нет)
                  (SELECT count(*) FROM product_catalog p WHERE p.seller_id = s.id AND p.on_sale AND NOT p.blocked) AS products
           FROM sellers s WHERE s.id = %s""", (seller_id,)).fetchone()
    if seller is None:
        return None
    seller["city"] = seller["city"] or seller["city_guess"]
    seller["phone_list"] = phone_list(_phones(conn, seller))
    seller["divisions"] = conn.execute(
        """SELECT division, count(*) FROM product_catalog WHERE seller_id = %s AND division IS NOT NULL
           AND on_sale AND NOT blocked GROUP BY 1 ORDER BY 2 DESC""", (seller_id,)).fetchall()
    return seller


KINDS = {"own": "Свои магазины", "found": "Найдены в интернете"}
CHOICES_TTL = 600  # города и категории для фильтра меняются редко, а страница — на каждый щелчок фильтра
_choices_cache: tuple[float, tuple[list, list]] | None = None


def _choices(conn: psycopg.Connection) -> tuple[list[str], list[str]]:
    """Города (свой продавца или самый частый у его товаров) и разделы товаров — для фильтра."""
    global _choices_cache
    if _choices_cache and _choices_cache[0] > time.monotonic():
        return _choices_cache[1]
    cities = [r[0] for r in conn.execute(
        """SELECT city, count(*) FROM (
               SELECT coalesce(s.city, mode() WITHIN GROUP (ORDER BY p.city)) AS city
               FROM sellers s LEFT JOIN product_catalog p ON p.seller_id = s.id AND p.on_sale AND NOT p.blocked
               WHERE s.status = 'active' GROUP BY s.id HAVING count(p.id) > 0 OR s.kind = 'own') x
           WHERE city IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 30""")]
    divisions = [r[0] for r in conn.execute(
        """SELECT p.division FROM sellers s
           JOIN product_catalog p ON p.seller_id = s.id AND p.on_sale AND NOT p.blocked
           WHERE s.status = 'active' AND p.division IS NOT NULL GROUP BY 1 ORDER BY count(DISTINCT s.id) DESC, 1""")]
    _choices_cache = (time.monotonic() + CHOICES_TTL, (cities, divisions))
    return cities, divisions


def listing(conn: psycopg.Connection, q: str = "", city: str = "", division: str = "",
            sort: str = "products", page: int = 1, verified: bool = False, kind: str = "") -> dict[str, Any]:
    """Список продавцов: поиск по названию (или БИН, сайту), город, категория товаров, только
    проверенные, свой магазин или найден в интернете; только с товарами (или свои магазины)
    и не скрытые."""
    sort = sort if sort in SORTS else "products"
    kind = kind if kind in KINDS else ""
    where, having, params = ["s.status = 'active'"], ["(count(p.id) > 0 OR s.kind = 'own')"], {}
    if q.strip():
        where.append("(s.name ILIKE %(like)s OR s.domain ILIKE %(like)s OR s.bin = %(q)s)")
        params["like"] = "%" + q.strip().replace("%", "").replace("_", "") + "%"
        params["q"] = q.strip()
    if verified:
        where.append("s.verified")
    if kind:
        where.append("s.kind = %(kind)s")
        params["kind"] = kind
    if city:
        having.append("coalesce(s.city, mode() WITHIN GROUP (ORDER BY p.city)) = %(city)s")
        params["city"] = city
    if division:
        having.append("bool_or(p.division = %(division)s)")
        params["division"] = division
    base = f"""SELECT s.id, s.name, s.kind, s.verified, s.domain, s.created_at, count(p.id) AS products,
                      coalesce(s.city, mode() WITHIN GROUP (ORDER BY p.city)) AS city,
                      (array_agg(p.division ORDER BY p.division) FILTER (WHERE p.division IS NOT NULL)) AS divisions
               FROM sellers s LEFT JOIN product_catalog p ON p.seller_id = s.id AND p.on_sale AND NOT p.blocked
               WHERE {" AND ".join(where)} GROUP BY s.id HAVING {" AND ".join(having)}"""
    cur = conn.cursor(row_factory=dict_row)
    total = cur.execute(f"SELECT count(*) AS n FROM ({base}) x", params).fetchone()["n"]
    rows = cur.execute(f"{base} ORDER BY {_ORDER[sort]} LIMIT {PAGE_SIZE} OFFSET {(max(page, 1) - 1) * PAGE_SIZE}",
                       params).fetchall()
    for row in rows:
        # Главные категории продавца — по числу товаров, три первых.
        counts: dict[str, int] = {}
        for code in row["divisions"] or []:
            counts[code] = counts.get(code, 0) + 1
        row["top"] = sorted(counts, key=lambda c: -counts[c])[:3]
    cities, divisions = _choices(conn)
    return {"rows": rows, "total": total, "page": max(page, 1), "pages": max(1, -(-total // PAGE_SIZE)),
            "sort": sort, "q": q, "city": city, "division": division, "cities": cities, "divisions": divisions,
            "verified": verified, "kind": kind}


# Продавцы для sitemap — как в списке /sellers: на виду и с товарами (или свой магазин).
_SITEMAP = """FROM sellers s WHERE s.status = 'active' AND (s.kind = 'own' OR EXISTS (
                  SELECT 1 FROM product_catalog p WHERE p.seller_id = s.id AND p.on_sale AND NOT p.blocked))"""


def sitemap_count(conn: psycopg.Connection) -> int:
    return conn.execute(f"SELECT count(*) {_SITEMAP}").fetchone()[0]


def sitemap_rows(conn: psycopg.Connection, offset: int, limit: int) -> list[tuple[str, Any]]:
    return [(path({"id": sid, "name": name}), updated) for sid, name, updated in conn.execute(
        f"SELECT s.id, s.name, s.updated_at {_SITEMAP} ORDER BY s.id LIMIT %s OFFSET %s", (limit, offset))]
