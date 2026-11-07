"""Подробности открытых объявлений из GraphQL: признаки, места поставки, аванс.

В REST-списке их нет, а фильтры на сайте и подписки на них опираются. Берём
пачками по 50 объявлений: GraphQL принимает список id.
"""

import json
import logging
from typing import Any

import psycopg

from zakup.goszakup import Client, parse_date

logger = logging.getLogger(__name__)

BATCH = 50
QUERY = """{ TrdBuy(limit: %(limit)d, filter: {id: %(ids)s}) {
  id kato disablePersonId refTypeTradeId refSubjectTypeId countLots customerBin customerNameRu
  lastUpdateDate
  Lots { id plnPointKatoList Plans { prepayment PlansKato { refKatoCode } } }
} }"""


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse(item: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[int, float | None, list[str]]]]:
    """Разбирает объявление из GraphQL: поля объявления и (id, аванс, КАТО) по лотам."""
    kato: set[str] = set(item.get("kato") or [])
    lots = []
    for lot in item.get("Lots") or []:
        lot_kato = set(lot.get("plnPointKatoList") or [])
        prepayments = []
        for plan in lot.get("Plans") or []:
            if (value := _number(plan.get("prepayment"))) is not None:
                prepayments.append(value)
            lot_kato.update(k["refKatoCode"] for k in plan.get("PlansKato") or [] if k.get("refKatoCode"))
        lot_kato.discard("")
        kato |= lot_kato
        lots.append((int(lot["id"]), max(prepayments) if prepayments else None, sorted(lot_kato)))
    prepayment_values = [p for _, p, _ in lots if p is not None]
    fields = {
        "id": int(item["id"]),
        "disable_person": item.get("disablePersonId") == 1,
        "kato": sorted(k for k in kato if k),
        "prepayment_max": max(prepayment_values) if prepayment_values else None,
        "type_trade_id": item.get("refTypeTradeId"),
        "subject_type_id": item.get("refSubjectTypeId"),
        "count_lots": item.get("countLots"),
        "customer_bin": item.get("customerBin"),
        "customer_name_ru": item.get("customerNameRu"),
        "last_update_date": parse_date(item.get("lastUpdateDate")),
    }
    return fields, lots


def save(conn: psycopg.Connection, fields: dict[str, Any], lots: list) -> None:
    conn.execute(
        """UPDATE announcements SET
               disable_person = %(disable_person)s, kato = %(kato)s,
               prepayment_max = %(prepayment_max)s, type_trade_id = %(type_trade_id)s,
               subject_type_id = %(subject_type_id)s,
               count_lots = coalesce(%(count_lots)s, count_lots),
               customer_bin = coalesce(%(customer_bin)s, customer_bin),
               customer_name_ru = coalesce(%(customer_name_ru)s, customer_name_ru),
               details_fetched_at = now(),
               details_update_date = coalesce(%(last_update_date)s, last_update_date)
           WHERE id = %(id)s""",
        fields,
    )
    for lot_id, prepayment, kato in lots:
        conn.execute(
            "UPDATE lots SET prepayment = %s, kato = %s WHERE id = %s", (prepayment, kato, lot_id)
        )


def refresh(conn: psycopg.Connection, client: Client, limit: int) -> int:
    """Дочитывает подробности открытых объявлений: новых и изменившихся."""
    ids = [
        row[0]
        for row in conn.execute(
            """SELECT id FROM announcements
               WHERE end_date > now()
                 AND (details_fetched_at IS NULL
                      OR last_update_date > details_update_date
                      OR details_fetched_at < now() - interval '1 day')
               ORDER BY details_fetched_at NULLS FIRST, end_date
               LIMIT %s""",
            (limit,),
        )
    ]
    done = 0
    for start in range(0, len(ids), BATCH):
        batch = ids[start : start + BATCH]
        data = client.graphql(QUERY % {"limit": len(batch), "ids": json.dumps(batch)})
        found = data.get("TrdBuy") or []
        for item in found:
            save(conn, *parse(item))
        # Объявления, которых GraphQL не вернул, не перечитываем по кругу до завтра.
        missing = set(batch) - {int(item["id"]) for item in found}
        if missing:
            conn.execute(
                "UPDATE announcements SET details_fetched_at = now() WHERE id = ANY(%s)",
                (list(missing),),
            )
        conn.commit()
        done += len(found)
    return done
