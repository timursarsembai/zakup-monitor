"""Свой магазин пользователя в кабинете (docs/CATALOG_PLAN.md, этап 3).

Магазин — строка sellers (kind = 'own', user_id); товары — в product_catalog (own = true),
цены по количеству — product_prices (розница — min_qty = 1, она же product_catalog.price).

Проверка: БИН/ИИН есть в реестре юрлиц (data.egov.kz, legal_entities) или среди участников
госзакупок (subjects) — значок «проверен» и магазин виден сразу; нет БИН или нет в реестре —
ждёт сотрудника (status = 'pending'). Скрытый сотрудником магазин сам владелец не откроет.

«Это мой магазин» — заявка на найденный ИИ магазин (seller_claims): сотрудник одобряет —
магазин переходит пользователю (его прежний свой магазин сливается в него).
"""

import re
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from zakup import catalog, contacts, legal_entities, products

MAX_PRODUCTS = 2000
MAX_PHONES = 3
MAX_TIERS = 10
MAX_SPECS = 30
PAGE_SIZE = 50
UNITS = ("шт", "упак", "комплект", "пара", "кг", "г", "т", "л", "м", "пог. м", "м²", "м³", "рулон", "лист",
         "мешок", "коробка", "услуга")
_ENSTRU = re.compile(r"^\d{6}(\.\d{3}(\.\d{6})?)?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PRICE_MAX = Decimal("1e12")


class Invalid(ValueError):
    """Ошибка в форме — текст для пользователя (переводится в шаблоне)."""


def _text(value: str | None, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def _number(value: str | None, what: str) -> Decimal | None:
    if not value or not value.strip():
        return None
    try:
        number = Decimal(value.replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise Invalid(f"{what}: нужно число") from None
    if not number.is_finite() or number <= 0 or number >= _PRICE_MAX:
        raise Invalid(f"{what}: нужно число больше нуля")
    return number


def _bin(value: str | None) -> str | None:
    value = (value or "").strip()
    if value and not (value.isdigit() and len(value) == 12):
        raise Invalid("БИН/ИИН — 12 цифр.")
    return value or None


def _sync_products(conn: psycopg.Connection, seller: dict[str, Any]) -> None:
    """Название, город и телефон магазина — и в его товарах (поиск по продавцу, таблица)."""
    conn.execute("""UPDATE product_catalog SET seller = %s, city = %s, phone = %s, updated_at = now()
                    WHERE seller_id = %s AND own""",
                 (seller["name"], seller["city"], (seller["phones"] or [{}])[0].get("number"), seller["id"]))


# --- магазин ---------------------------------------------------------------------------------

def get(conn: psycopg.Connection, user: int) -> dict[str, Any] | None:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM sellers WHERE user_id = %s", (user,)).fetchone()


def parse_shop(*, name: str, phones: list[str], whatsapp: str = "", email: str = "", website: str = "",
               city: str = "", regions: str = "", description: str = "", bin: str = "",
               prices_with_vat: str = "") -> dict[str, Any]:
    name = _text(name, 150)
    if not name:
        raise Invalid("Укажите название магазина.")
    numbers: list[dict] = []
    for raw in phones[:MAX_PHONES]:
        if not raw.strip():
            continue
        number = contacts.normalize(raw)
        if number is None:
            raise Invalid("Телефон — в формате +7 (7xx) xxx-xx-xx.")
        if all(p["number"] != number for p in numbers):
            numbers.append({"number": number, "label": "", "whatsapp": False})
    if not numbers:
        raise Invalid("Укажите хотя бы один телефон.")
    wa = contacts.normalize(whatsapp) if whatsapp.strip() else None
    if whatsapp.strip() and wa is None:
        raise Invalid("WhatsApp — в формате +7 (7xx) xxx-xx-xx.")
    email = email.strip()[:200]
    if email and not _EMAIL.match(email):
        raise Invalid("Проверьте e-mail.")
    website = website.strip()[:300]
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    if website and not contacts._domain(website):
        raise Invalid("Проверьте адрес сайта.")
    bin = _bin(bin)
    return {"name": name, "phones": numbers, "whatsapp": wa, "email": email or None, "website": website or None,
            "city": _text(city, 80) or None,
            "regions": [r for r in (_text(x, 80) for x in regions.split(",")) if r][:20],
            "description": (description or "").strip()[:3000] or None, "bin": bin,
            "prices_with_vat": prices_with_vat == "1"}


def registry_name(conn: psycopg.Connection, bin_: str | None, http: httpx.Client | None = None) -> str | None:
    """Название по БИН/ИИН из реестра юрлиц или участников госзакупок; None — не нашли.
    Реестр спрашиваем по сети (коммитит соединение) — звать до своих изменений в базе."""
    if not bin_:
        return None
    own = http is None
    http = http or httpx.Client(timeout=httpx.Timeout(8, connect=3))
    try:
        legal_entities.ensure(conn, [bin_], http=http)
    finally:
        if own:
            http.close()
    row = conn.execute(
        """SELECT coalesce((SELECT name_ru FROM legal_entities WHERE bin = %(b)s),
                           (SELECT name_ru FROM subjects WHERE bin = %(b)s))""", {"b": bin_}).fetchone()
    return row[0] if row else None


def save(conn: psycopg.Connection, user: int, data: dict[str, Any], registry: str | None) -> dict[str, Any]:
    """Создаёт или меняет магазин пользователя. registry — название по БИН из реестра (или None).
    Возвращает строку магазина."""
    current = conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM sellers WHERE user_id = %s FOR UPDATE", (user,)).fetchone()
    same_bin = current is not None and current["bin"] == data["bin"]
    verified = bool(data["bin"]) and (registry is not None or (same_bin and current["verified"]))
    if current is not None and current["status"] == "hidden":
        status = "hidden"
    elif verified or (same_bin and current["status"] == "active"):
        # Проверен по реестру — или раньше одобрен сотрудником с тем же БИН.
        status = "active"
    else:
        status = "pending"
    values = {**data, "phones": Jsonb(data["phones"]), "verified": verified, "status": status, "user": user}
    if current is None:
        row = conn.cursor(row_factory=dict_row).execute(
            """INSERT INTO sellers (kind, user_id, name, bin, verified, verified_at, status, phones, whatsapp,
                                    email, website, city, regions, description, prices_with_vat)
               VALUES ('own', %(user)s, %(name)s, %(bin)s, %(verified)s, CASE WHEN %(verified)s THEN now() END,
                       %(status)s, %(phones)s, %(whatsapp)s, %(email)s, %(website)s, %(city)s, %(regions)s,
                       %(description)s, %(prices_with_vat)s)
               RETURNING *""", values).fetchone()
    else:
        row = conn.cursor(row_factory=dict_row).execute(
            """UPDATE sellers SET name = %(name)s, bin = %(bin)s, verified = %(verified)s,
                      verified_at = CASE WHEN %(verified)s THEN coalesce(verified_at, now()) END,
                      status = %(status)s, phones = %(phones)s, whatsapp = %(whatsapp)s, email = %(email)s,
                      website = %(website)s, city = %(city)s, regions = %(regions)s,
                      description = %(description)s, prices_with_vat = %(prices_with_vat)s, updated_at = now()
               WHERE user_id = %(user)s RETURNING *""", values).fetchone()
        _sync_products(conn, row)
    return row


# --- товары ----------------------------------------------------------------------------------

def parse_specs(text: str) -> list[dict[str, str]]:
    """«Цвет: белый» по строке → [{"name": "Цвет", "value": "белый"}]."""
    specs = []
    for line in (text or "").splitlines():
        name, sep, value = line.partition(":")
        name, value = _text(name, 100), _text(value, 300)
        if sep and name and value:
            specs.append({"name": name, "value": value})
        elif line.strip():
            raise Invalid("Характеристики — по строке «название: значение».")
    if len(specs) > MAX_SPECS:
        raise Invalid(f"Характеристик — не больше {MAX_SPECS}.")
    return specs


def parse_tiers(retail: str, quantities: list[str], prices: list[str]) -> list[tuple[Decimal, Decimal]]:
    """Розница и оптовые ступени «от N — цена за единицу»: [(1, розница), (N, цена), …].
    Количество растёт, цена не растёт."""
    base = _number(retail, "Розничная цена")
    if base is None:
        raise Invalid("Укажите розничную цену.")
    tiers = [(Decimal(1), base)]
    rows = []
    for qty, price in zip(quantities, prices, strict=False):
        if not (qty or "").strip() and not (price or "").strip():
            continue
        n, p = _number(qty, "Оптом от, количество"), _number(price, "Оптовая цена")
        if n is None or p is None:
            raise Invalid("У оптовой цены укажите и количество, и цену.")
        if n <= 1:
            raise Invalid("Оптовая цена — от 2 единиц и больше.")
        rows.append((n, p))
    if len(rows) > MAX_TIERS:
        raise Invalid(f"Оптовых ступеней — не больше {MAX_TIERS}.")
    for n, p in sorted(rows):
        if n == tiers[-1][0]:
            raise Invalid("Две оптовые цены на одно количество.")
        if p > tiers[-1][1]:
            raise Invalid("Оптовая цена не может быть выше цены за меньшее количество.")
        tiers.append((n, p))
    return tiers


def parse_product(*, name: str, unit: str, retail: str, quantities: list[str], prices: list[str],
                  category: str = "", enstru: str = "", description: str = "", specs: str = "",
                  available: str = "") -> dict[str, Any]:
    name = _text(name, 300)
    if not name:
        raise Invalid("Укажите название товара.")
    unit = _text(unit, 30)
    if not unit:
        raise Invalid("Укажите единицу измерения.")
    enstru = enstru.strip()
    if enstru and not _ENSTRU.match(enstru):
        raise Invalid("Код ЕНС ТРУ — как 139224.992.000000 (или первые 6 цифр).")
    division = category if category in catalog.DIVISIONS else (enstru[:2] if enstru[:2] in catalog.DIVISIONS else None)
    return {"name": name, "unit": unit, "tiers": parse_tiers(retail, quantities, prices), "division": division,
            "enstru": enstru or None, "description": (description or "").strip()[:5000] or None,
            "specs": parse_specs(specs), "available": available == "1"}


def save_product(conn: psycopg.Connection, seller: dict[str, Any], data: dict[str, Any],
                 product_id: int | None = None) -> int | None:
    """Новый товар магазина или правка своего. None — нет такого товара у магазина."""
    retail = data["tiers"][0][1]
    values = {"name": data["name"], "seller": seller["name"], "seller_id": seller["id"], "city": seller["city"],
              "phone": (seller["phones"] or [{}])[0].get("number"), "price": retail, "unit": data["unit"],
              "specs": Jsonb(data["specs"]), "division": data["division"], "enstru": data["enstru"],
              "description": data["description"], "available": data["available"], "id": product_id}
    if product_id is None:
        count = conn.execute("SELECT count(*) FROM product_catalog WHERE seller_id = %s AND own",
                             (seller["id"],)).fetchone()[0]
        if count >= MAX_PRODUCTS:
            raise Invalid(f"В магазине — не больше {MAX_PRODUCTS} товаров.")
        product_id = conn.execute(
            """INSERT INTO product_catalog (url_key, name, seller, seller_id, city, phone, price, price_unit, specs,
                                            division, enstru, description, available, own)
               VALUES (%(key)s, %(name)s, %(seller)s, %(seller_id)s, %(city)s, %(phone)s, %(price)s, %(unit)s,
                       %(specs)s, %(division)s, %(enstru)s, %(description)s, %(available)s, true)
               RETURNING id""", {**values, "key": "own:" + secrets.token_hex(8)}).fetchone()[0]
    else:
        row = conn.execute(
            """UPDATE product_catalog SET
                   -- прежняя цена — в историю (как у найденных товаров)
                   price_history = CASE WHEN price IS DISTINCT FROM %(price)s AND price IS NOT NULL
                       THEN (jsonb_build_array(jsonb_build_object(
                                 'at', to_char(checked_at AT TIME ZONE 'Asia/Almaty', 'YYYY-MM-DD"T"HH24:MI'),
                                 'price', price)) || price_history) - {HISTORY_MAX}
                       ELSE price_history END,
                   name = %(name)s, seller = %(seller)s, city = %(city)s, phone = %(phone)s, price = %(price)s,
                   price_unit = %(unit)s, specs = %(specs)s, division = %(division)s, enstru = %(enstru)s,
                   description = %(description)s, available = %(available)s, checked_at = now(), updated_at = now()
               WHERE id = %(id)s AND seller_id = %(seller_id)s AND own RETURNING id""".replace(
                "{HISTORY_MAX}", str(products.HISTORY_MAX)), values).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM product_prices WHERE product_id = %s", (product_id,))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO product_prices (product_id, min_qty, price) VALUES (%s, %s, %s)",
                        [(product_id, n, p) for n, p in data["tiers"]])
    return product_id


def tiers(conn: psycopg.Connection, product_id: int) -> list[tuple[Decimal, Decimal]]:
    return [tuple(r) for r in conn.execute(
        "SELECT min_qty, price FROM product_prices WHERE product_id = %s ORDER BY min_qty", (product_id,))]


def product(conn: psycopg.Connection, seller_id: int, product_id: int) -> dict[str, Any] | None:
    """Свой товар магазина для формы правки."""
    item = conn.cursor(row_factory=dict_row).execute(
        """SELECT id, name, price, price_unit, specs, division, enstru, description, available, on_sale
           FROM product_catalog WHERE id = %s AND seller_id = %s AND own""", (product_id, seller_id)).fetchone()
    if item is not None:
        item["tiers"] = tiers(conn, product_id)
    return item


def listing(conn: psycopg.Connection, seller_id: int, q: str = "", page: int = 1) -> dict[str, Any]:
    """Товары магазина для владельца: и снятые с продажи, и найденные ИИ (их только снять)."""
    where, params = "seller_id = %(seller)s", {"seller": seller_id}
    if q.strip():
        where += " AND name ILIKE %(like)s"
        params["like"] = "%" + q.strip().replace("%", "").replace("_", "") + "%"
    cur = conn.cursor(row_factory=dict_row)
    total = cur.execute(f"SELECT count(*) AS n FROM product_catalog WHERE {where}", params).fetchone()["n"]
    page = max(page, 1)
    rows = cur.execute(
        f"""SELECT id, name, price, price_unit, available, on_sale, own, division, updated_at,
                   (SELECT count(*) FROM product_prices pp WHERE pp.product_id = p.id AND pp.min_qty > 1) AS wholesale
            FROM product_catalog p WHERE {where} ORDER BY own DESC, updated_at DESC, id DESC
            LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}""", params).fetchall()
    return {"rows": rows, "total": total, "page": page, "pages": max(1, -(-total // PAGE_SIZE)), "q": q}


def set_on_sale(conn: psycopg.Connection, seller_id: int, product_id: int, on_sale: bool) -> bool:
    return conn.execute("UPDATE product_catalog SET on_sale = %s, updated_at = now() WHERE id = %s AND seller_id = %s",
                        (on_sale, product_id, seller_id)).rowcount == 1


def delete_product(conn: psycopg.Connection, seller_id: int, product_id: int) -> bool:
    """Удалить можно только свой товар; найденный ИИ — только снять с продажи (иначе
    следующий поиск добавит его снова)."""
    return conn.execute("DELETE FROM product_catalog WHERE id = %s AND seller_id = %s AND own",
                        (product_id, seller_id)).rowcount == 1


# --- «Это мой магазин» и модерация -----------------------------------------------------------

def claim(conn: psycopg.Connection, seller_id: int, user: int, bin_: str, comment: str) -> bool:
    """Заявка на найденный магазин. False — магазин не найденный или уже чей-то."""
    bin_ = _bin(bin_)
    comment = (comment or "").strip()[:1000]
    if not comment:
        raise Invalid("Напишите, как подтвердить, что магазин ваш.")
    owned = conn.execute("SELECT kind = 'found' AND user_id IS NULL FROM sellers WHERE id = %s",
                         (seller_id,)).fetchone()
    if not owned or not owned[0]:
        return False
    conn.execute("""INSERT INTO seller_claims (seller_id, user_id, bin, comment) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (seller_id, user_id) WHERE status = 'pending' DO UPDATE
                    SET bin = excluded.bin, comment = excluded.comment, created_at = now()""",
                 (seller_id, user, bin_, comment))
    return True


def pending_claim(conn: psycopg.Connection, seller_id: int, user: int | None) -> bool:
    return bool(user) and conn.execute(
        "SELECT 1 FROM seller_claims WHERE seller_id = %s AND user_id = %s AND status = 'pending'",
        (seller_id, user)).fetchone() is not None


def decide_claim(conn: psycopg.Connection, claim_id: int, staff: int, approve: bool) -> str:
    """Решение сотрудника. Одобрено — магазин пользователю (его свой магазин сливается сюда),
    остальные заявки на этот магазин отклоняются. Возвращает итог: approved, rejected или gone."""
    row = conn.execute("""SELECT c.seller_id, c.user_id, c.bin, s.user_id FROM seller_claims c
                          JOIN sellers s ON s.id = c.seller_id
                          WHERE c.id = %s AND c.status = 'pending' FOR UPDATE OF c, s""", (claim_id,)).fetchone()
    if row is None:
        return "gone"
    seller_id, user, bin_, owner = row
    if not approve or owner is not None:
        conn.execute("UPDATE seller_claims SET status = 'rejected', decided_at = now(), decided_by = %s WHERE id = %s",
                     (staff, claim_id))
        return "rejected"
    old = conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM sellers WHERE user_id = %s FOR UPDATE", (user,)).fetchone()
    if old is not None:
        conn.execute("UPDATE product_catalog SET seller_id = %s WHERE seller_id = %s", (seller_id, old["id"]))
        conn.execute("DELETE FROM sellers WHERE id = %s", (old["id"],))
    keep = old or {}
    # БИН — из заявки, иначе прежнего магазина, иначе найденного; проверен — по реестру
    # (или прежний магазин уже был проверен с этим БИН).
    current_bin = conn.execute("SELECT bin FROM sellers WHERE id = %s", (seller_id,)).fetchone()[0]
    bin_ = bin_ or keep.get("bin") or current_bin
    verified = bool(bin_) and ((keep.get("bin") == bin_ and bool(keep.get("verified"))) or conn.execute(
        "SELECT EXISTS (SELECT 1 FROM legal_entities WHERE bin = %(b)s AND name_ru IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM subjects WHERE bin = %(b)s)", {"b": bin_}).fetchone()[0])
    # Скрытый модератором магазин заявкой на другой не вернуть в каталог.
    status = "hidden" if keep.get("status") == "hidden" else "active"
    seller = conn.cursor(row_factory=dict_row).execute(
        """UPDATE sellers SET user_id = %(user)s, kind = 'own', status = %(status)s,
                  bin = %(bin)s, verified = %(verified)s,
                  verified_at = CASE WHEN %(verified)s THEN now() END,
                  phones = CASE WHEN %(phones)s::jsonb <> '[]' THEN %(phones)s::jsonb ELSE phones END,
                  whatsapp = coalesce(%(whatsapp)s, whatsapp), email = coalesce(%(email)s, email),
                  website = coalesce(website, %(website)s), city = coalesce(%(city)s, city),
                  description = coalesce(%(description)s, description),
                  prices_with_vat = coalesce(%(vat)s, prices_with_vat), updated_at = now()
           WHERE id = %(id)s RETURNING *""",
        {"user": user, "bin": bin_, "status": status, "verified": verified, "id": seller_id,
         "phones": Jsonb(keep.get("phones") or []), "whatsapp": keep.get("whatsapp"), "email": keep.get("email"),
         "website": keep.get("website"), "city": keep.get("city"), "description": keep.get("description"),
         "vat": keep.get("prices_with_vat")}).fetchone()
    _sync_products(conn, seller)
    conn.execute("UPDATE seller_claims SET status = 'approved', decided_at = now(), decided_by = %s WHERE id = %s",
                 (staff, claim_id))
    conn.execute("""UPDATE seller_claims SET status = 'rejected', decided_at = now(), decided_by = %s
                    WHERE seller_id = %s AND status = 'pending'""", (staff, seller_id))
    return "approved"


def moderation(conn: psycopg.Connection) -> dict[str, Any]:
    """Сотруднику: магазины на проверке, заявки «Это мой магазин», скрытые."""
    cur = conn.cursor(row_factory=dict_row)
    return {
        "pending": cur.execute(
            """SELECT s.id, s.name, s.bin, s.city, s.created_at, u.email AS login,
                      (SELECT count(*) FROM product_catalog p WHERE p.seller_id = s.id) AS products
               FROM sellers s LEFT JOIN users u ON u.id = s.user_id
               WHERE s.status = 'pending' ORDER BY s.created_at""").fetchall(),
        "claims": cur.execute(
            """SELECT c.id, c.bin, c.comment, c.created_at, s.id AS seller_id, s.name, s.domain, u.email AS login,
                      (SELECT name_ru FROM legal_entities WHERE bin = c.bin) AS registry
               FROM seller_claims c JOIN sellers s ON s.id = c.seller_id JOIN users u ON u.id = c.user_id
               WHERE c.status = 'pending' ORDER BY c.created_at""").fetchall(),
        "hidden": cur.execute(
            """SELECT s.id, s.name, s.kind, s.updated_at FROM sellers s WHERE s.status = 'hidden'
               ORDER BY s.updated_at DESC LIMIT 100""").fetchall(),
    }


def set_status(conn: psycopg.Connection, seller_id: int, status: str) -> bool:
    if status not in ("active", "hidden"):
        return False
    return conn.execute("UPDATE sellers SET status = %s, updated_at = now() WHERE id = %s",
                        (status, seller_id)).rowcount == 1
