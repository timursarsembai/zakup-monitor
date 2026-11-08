"""Обработка ответа ИИ: чистка ссылок и чисел, телефоны, объединение с прошлым поиском, отсев без маржи."""


import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


def clean(result: dict[str, Any]) -> dict[str, Any]:
    """Только http(s)-ссылки и числа там, где ждём числа: ответ модели — чужие данные."""
    items = []
    for item in result.get("items") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        image = str(item.get("image") or "")
        price, unit_price = _number(item.get("price")), _number(item.get("unit_price"))
        price_unit = str(item.get("price_unit") or "").strip()
        note = str(item.get("note") or "").strip()
        estimated = False
        if unit_price is None and price is not None:
            # Единицу ИИ не сопоставил — считаем по цене со страницы, помечаем «≈».
            unit_price, estimated = price, True
        if len(price_unit) > 20:
            # В колонке цены — только число и короткая единица; пояснения — в заметку.
            note = f"{note} Цена: {price_unit}.".strip() if price_unit not in note else note
            price_unit = ""
        items.append({
            "name": str(item.get("name") or "")[:300],
            "seller": str(item.get("seller") or "")[:150],
            "city": str(item.get("city") or "")[:100],
            "in_region": bool(item.get("in_region")),
            "price": price,
            "price_unit": price_unit[:20],
            "unit_price": unit_price,
            "unit_estimated": estimated,
            "url": url[:1000],
            "phone": _phone(item.get("phone")),
            "image": image[:1000] if image.startswith(("https://", "http://")) else None,
            "fit": str(item.get("fit") or "")[:20],
            "matches": _strings(item.get("matches")),
            "mismatches": _strings(item.get("mismatches")),
            "note": note[:500],
        })
    items.sort(key=lambda i: (not i["in_region"], i["unit_price"] is None, i["unit_price"] or 0))
    return {
        "region_searched": str(result.get("region_searched") or "")[:200],
        "category": str(result.get("category") or "")[:60],
        "summary": str(result.get("summary") or "")[:2000],
        "items": items[:30],
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:120] for v in value if str(v).strip()][:10]


def _phone(value: Any) -> str | None:
    """Только похожее на телефоны: цифры, +, скобки, дефисы, пробелы, запятые."""
    text = str(value or "").strip()
    if not text or not any(c.isdigit() for c in text):
        return None
    cleaned = "".join(c for c in text if c.isdigit() or c in "+()-, ")
    return " ".join(cleaned.split())[:120] or None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _base_result(conn: psycopg.Connection, job: dict[str, Any]) -> dict[str, Any] | None:
    if not job.get("base_id"):
        return None
    row = conn.execute("SELECT result FROM lot_research WHERE id = %s", (job["base_id"],)).fetchone()
    return row[0] if row else None


def merge(base: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    """«Найти ещё»: к прошлому результату — только новые ссылки. added = 0 — больше нет."""
    known = {i["url"] for i in base.get("items", [])}
    new = [{**i, "new": True} for i in found.get("items", []) if i["url"] not in known]
    # Прежние — на своих местах, новые — в конце: не перемешиваем.
    items = [{k: v for k, v in i.items() if k != "new"} for i in base.get("items", [])] + new
    return {**base, "items": items, "added": len(new), "exhausted": not new,
            "category": base.get("category") or found.get("category", "")}


def without_loss(result: dict[str, Any], lot_price: Any) -> dict[str, Any]:
    """Убирает предложения не дешевле цены лота за единицу: в них нет маржи.
    Без цены у продавца (не удалось узнать) — оставляем, решит человек."""
    try:
        limit = float(lot_price)
    except (TypeError, ValueError):
        return result
    if limit <= 0:
        return result
    kept = [i for i in result.get("items") or []
            if not isinstance(i.get("unit_price"), (int, float)) or i["unit_price"] < limit]
    return {**result, "items": kept, "dropped_no_margin": len(result.get("items") or []) - len(kept)}
