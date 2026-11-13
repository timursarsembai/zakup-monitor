"""Данные лота для поиска: техспецификация, место поставки, регион по умолчанию, ссылки для ручного поиска."""


import logging
from typing import Any
from urllib.parse import quote_plus

import psycopg

from zakup import details
from zakup.research.prompts import (
    SPEC_LIMIT,
)

logger = logging.getLogger(__name__)


def lot_context(conn: psycopg.Connection, lot_id: int) -> dict[str, Any] | None:
    """Данные лота для запроса: из подробностей объявления и кэша спецификаций."""
    row = conn.execute(
        """SELECT a.detail, l.trd_buy_id FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
           WHERE l.id = %s""",
        (lot_id,),
    ).fetchone()
    if row is None or not row[0]:
        return None
    lot = next((x for x in row[0].get("Lots") or [] if x.get("id") == lot_id), None)
    if lot is None:
        return None
    info = details.parse_lot(lot)
    places = []
    for plan in lot.get("Plans") or []:
        for kato in plan.get("PlansKato") or []:
            place = (kato.get("fullDeliveryPlaceNameRu") or "").strip()
            if place and place not in places:
                places.append(place)
    kato = [k for k in info["kato"]] or list(row[0].get("kato") or [])
    spec = conn.execute("SELECT text FROM lot_specs WHERE lot_id = %s", (lot_id,)).fetchone()
    count = lot.get("count") or 0
    price = info["price"]
    if price is None and count:
        price = round((lot.get("amount") or 0) / count, 2)
    return {
        "name": lot.get("nameRu") or "",
        "description": lot.get("descriptionRu") or "",
        "extra": info["extra_desc"] or "",
        "count": count,
        "unit": (info["unit_name"] or "единица").lower(),
        "price": price,
        "places": "; ".join(places) or "не указано",
        "kato": kato,
        "spec": (spec[0] if spec and spec[0] else "нет — ориентируйся на описание")[:SPEC_LIMIT],
        # Без техспецификации поиску товаров не по чему искать (вкладка скрыта). Есть файл, но
        # он ещё не разобран или не разбирается — поиск есть (ориентируется на описание).
        "has_spec": bool(info["spec_url"] or (spec and spec[0])),
    }


# Города республиканского значения и областные центры (по первым цифрам КАТО).
# Районы и сёла в поиске не нужны: там почти никогда нет магазинов.
REPUBLICAN = {"71": "г. Астана", "75": "г. Алматы", "79": "г. Шымкент"}


CENTERS = {
    "10": ("Семей",), "11": ("Кокшетау", "Көкшетау"), "15": ("Актобе", "Ақтөбе"),
    "19": ("Конаев", "Қонаев", "Капшагай"), "23": ("Атырау",), "27": ("Уральск", "Орал"),
    "31": ("Тараз",), "33": ("Талдыкорган", "Талдықорған"), "35": ("Караганда", "Қарағанды"),
    "39": ("Костанай", "Қостанай"), "43": ("Кызылорда", "Қызылорда"), "47": ("Актау", "Ақтау"),
    "55": ("Павлодар",), "59": ("Петропавловск", "Петропавл"), "61": ("Туркестан", "Түркістан"),
    "62": ("Жезказган", "Жезқазған"), "63": ("Усть-Каменогорск", "Өскемен"),
}


def default_region(context: dict[str, Any]) -> str:
    """Где искать сначала: город республиканского значения; областной центр и его
    область; иначе — только область (без района и села)."""
    from zakup.filters import REGIONS

    code = next((k[:2] for k in context.get("kato") or [] if k[:2] in REGIONS), None)
    if code is None:
        return "Казахстан"
    if code in REPUBLICAN:
        return REPUBLICAN[code]
    place = context.get("places", "").lower()
    for center in CENTERS.get(code, ()):
        if center.lower() in place:
            return f"г. {CENTERS[code][0]}, {REGIONS[code]}"
    return REGIONS[code]


def manual_links(context: dict[str, Any] | None, region: str) -> list[tuple[str, str]]:
    """Ссылки «искать самому»: готовые поисковые запросы по названию лота, без ИИ."""
    if not context or not context.get("name"):
        return []
    words = " ".join(f"{context['name']} {context.get('extra') or ''}".split())[:120]
    city = region.replace("г.", "").replace("область", "").strip(" ,.")
    q = quote_plus
    return [
        ("Google — в регионе", f"https://www.google.com/search?q={q(f'{words} купить {city} цена')}"),
        ("Google — по Казахстану", f"https://www.google.com/search?q={q(f'{words} купить Казахстан цена')}"),
        ("Kaspi", f"https://kaspi.kz/shop/search/?text={q(words)}"),
        ("Satu", f"https://satu.kz/search?search_term={q(words)}"),
        ("Halyk Market", f"https://halykmarket.kz/search?r46_search_query={q(words)}"),
        ("Flip.kz", f"https://www.flip.kz/search?search={q(words)}"),
    ]


def clean_region(region: str) -> str:
    """Регион — короткий текст: буквы, цифры, пробелы и знаки препинания, без переносов.
    Попадает в подсказку ИИ: длинный текст был бы уже не регионом, а указаниями."""
    allowed = "".join(c for c in region if c.isalnum() or c in " .,-()/")
    return " ".join(allowed.split())[:REGION_LIMIT].strip() or "Казахстан"


REGION_LIMIT = 60
