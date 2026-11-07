"""Подробности объявлений из GraphQL: справочники, организатор, лоты, файлы.

В REST-списке этого нет, а фильтры, подписки и страница объявления на них
опираются. Шаг `zakup details` берёт открытые объявления пачками по 50,
страница объявления перечитывает своё при просмотре (fetch_one).
"""

import json
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zakup import db
from zakup.goszakup import Client, normalize, parse_date
from zakup.spec import spec_file

logger = logging.getLogger(__name__)

BATCH = 50
FIELDS = """
  id numberAnno nameRu nameKz totalSum countLots publishDate startDate endDate lastUpdateDate
  refBuyStatusId refTradeMethodsId kato disablePersonId refTypeTradeId refSubjectTypeId
  finYear itogiDatePublic
  customerBin customerNameRu customerNameKz orgBin orgNameRu orgNameKz
  RefBuyStatus { nameRu nameKz } RefTradeMethods { nameRu nameKz } RefTypeTrade { nameRu nameKz }
  RefSubjectType { nameRu nameKz }
  Organizer { bin nameRu nameKz Address { address katoCode } }
  Files { id nameRu nameKz originalName filePath }
  GeneralFiles { nameRu filePathRu }
  Lots {
    id lotNumber nameRu nameKz descriptionRu descriptionKz amount count trdBuyId trdBuyNumberAnno
    refLotStatusId refTradeMethodsId customerBin customerNameRu customerNameKz plnPointKatoList
    lastUpdateDate isDeleted
    RefLotsStatus { nameRu nameKz }
    Files { id nameRu nameKz originalName filePath }
    Plans {
      price count amount sum1 sum2 sum3 prepayment supplyDateRu extraDescRu extraDescKz
      refEnstruCode RefUnits { nameRu nameKz } PlansKato { refKatoCode fullDeliveryPlaceNameRu fullDeliveryPlaceNameKz }
    }
  }
"""
BATCH_QUERY = "{ TrdBuy(limit: %(limit)d, filter: {id: %(ids)s}) { " + FIELDS + " } }"


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _years(value: Any) -> list[int]:
    values = value if isinstance(value, list) else [value]
    return sorted({int(v) for v in values if isinstance(v, int) or str(v).isdigit()})


def parse_lot(lot: dict[str, Any]) -> dict[str, Any]:
    kato = set(lot.get("plnPointKatoList") or [])
    prepayments, prices, units, extra, supply, enstru = [], [], [], [], [], []
    units_kz, extra_kz = [], []
    for plan in lot.get("Plans") or []:
        if (value := _number(plan.get("prepayment"))) is not None:
            prepayments.append(value)
        if (value := _number(plan.get("price"))) is not None:
            prices.append(value)
        if unit := (plan.get("RefUnits") or {}).get("nameRu"):
            units.append(unit)
        if unit_kz := (plan.get("RefUnits") or {}).get("nameKz"):
            units_kz.append(unit_kz)
        if plan.get("extraDescRu"):
            extra.append(plan["extraDescRu"].strip())
        if plan.get("extraDescKz"):
            extra_kz.append(plan["extraDescKz"].strip())
        if plan.get("refEnstruCode"):
            enstru.append(plan["refEnstruCode"])
        if plan.get("supplyDateRu"):
            supply.append(plan["supplyDateRu"].strip())
        kato.update(k["refKatoCode"] for k in plan.get("PlansKato") or [] if k.get("refKatoCode"))
    kato.discard("")
    return {
        "id": int(lot["id"]),
        "prepayment": max(prepayments) if prepayments else None,
        "kato": sorted(kato),
        # Пунктов плана в лоте бывает несколько с одной ценой; разные — цену не показываем.
        "price": prices[0] if len(set(prices)) == 1 else None,
        "unit_name": units[0] if units else None,
        "unit_name_kz": units_kz[0] if units_kz else None,
        "extra_desc_kz": "; ".join(dict.fromkeys(extra_kz)) or None,
        "extra_desc": "; ".join(dict.fromkeys(extra)) or None,
        "supply_date": "; ".join(dict.fromkeys(supply)) or None,
        "enstru": sorted(set(enstru)),
        "spec_url": spec_file(lot),
    }


def parse(item: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Разбирает объявление из GraphQL: поля объявления и поля его лотов."""
    lots = [parse_lot(lot) for lot in item.get("Lots") or [] if not lot.get("isDeleted")]
    kato = set(item.get("kato") or [])
    for lot in lots:
        kato.update(lot["kato"])
    prepayments = [lot["prepayment"] for lot in lots if lot["prepayment"] is not None]
    fields = {
        "id": int(item["id"]),
        "disable_person": item.get("disablePersonId") == 1,
        "kato": sorted(k for k in kato if k),
        "prepayment_max": max(prepayments) if prepayments else None,
        "type_trade_id": item.get("refTypeTradeId"),
        "subject_type_id": item.get("refSubjectTypeId"),
        "count_lots": item.get("countLots"),
        "customer_bin": item.get("customerBin"),
        "customer_name_ru": item.get("customerNameRu"),
        "last_update_date": parse_date(item.get("lastUpdateDate")),
        "fin_years": _years(item.get("finYear")),
        "itogi_date": parse_date(item.get("itogiDatePublic")),
    }
    return fields, lots


def save(conn: psycopg.Connection, item: dict[str, Any]) -> None:
    fields, lots = parse(item)
    conn.execute(
        """UPDATE announcements SET
               disable_person = %(disable_person)s, kato = %(kato)s,
               prepayment_max = %(prepayment_max)s, type_trade_id = %(type_trade_id)s,
               subject_type_id = %(subject_type_id)s,
               fin_years = %(fin_years)s, itogi_date = %(itogi_date)s,
               count_lots = coalesce(%(count_lots)s, count_lots),
               customer_bin = coalesce(%(customer_bin)s, customer_bin),
               customer_name_ru = coalesce(%(customer_name_ru)s, customer_name_ru),
               detail = %(detail)s, details_fetched_at = now(),
               details_update_date = coalesce(%(last_update_date)s, last_update_date)
           WHERE id = %(id)s""",
        {**fields, "detail": Jsonb(item)},
    )
    raw_lots = {int(lot["id"]): lot for lot in item.get("Lots") or []}
    for lot in lots:
        raw = {k: v for k, v in raw_lots[lot["id"]].items() if not k[:1].isupper()}
        # Лот мог ещё не дойти через REST: заводим его отсюда, REST потом дополнит.
        db.upsert_lot(conn, normalize(raw))
        conn.execute(
            """UPDATE lots SET prepayment = %(prepayment)s, kato = %(kato)s, price = %(price)s,
                   unit_name = %(unit_name)s, extra_desc = %(extra_desc)s,
                   supply_date = %(supply_date)s, enstru = %(enstru)s, spec_url = %(spec_url)s,
                   unit_name_kz = %(unit_name_kz)s, extra_desc_kz = %(extra_desc_kz)s
               WHERE id = %(id)s""",
            lot,
        )


def fetch_one(conn: psycopg.Connection, client: Client, announcement_id: int) -> bool:
    data = client.graphql(BATCH_QUERY % {"limit": 1, "ids": json.dumps([announcement_id])})
    found = data.get("TrdBuy") or []
    for item in found:
        save(conn, item)
    conn.commit()
    return bool(found)


def refresh(conn: psycopg.Connection, client: Client, limit: int) -> int:
    """Дочитывает подробности открытых объявлений: новых, изменившихся и раз в сутки."""
    ids = [
        row[0]
        for row in conn.execute(
            """SELECT id FROM announcements
               WHERE end_date > now()
                 AND (details_fetched_at IS NULL OR detail IS NULL
                      OR NOT detail ? 'nameKz'
                      OR last_update_date > details_update_date
                      OR details_fetched_at < now() - interval '1 day')
               ORDER BY details_fetched_at NULLS FIRST, end_date
               LIMIT %s""",
            (limit,),
        )
    ]
    return _fetch(conn, client, ids)


# Итоговые статусы: дальше объявление не меняется, подробности читаем один раз.
FINISHED_STATUSES = [350, 410, 430]


def refresh_finished(conn: psycopg.Connection, client: Client, limit: int) -> int:
    """Подробности завершённых объявлений: лоты с итоговыми статусами и протокол итогов.

    Перечитываем, пока статус в подробностях не совпадёт со статусом из REST.
    """
    ids = [
        row[0]
        for row in conn.execute(
            """SELECT id FROM announcements
               WHERE status_id = ANY(%s)
                 AND (detail IS NULL OR NOT detail ? 'nameKz'
                      OR (detail->>'refBuyStatusId')::int IS DISTINCT FROM status_id)
                 AND (details_fetched_at IS NULL OR details_fetched_at < now() - interval '1 hour')
               ORDER BY end_date DESC
               LIMIT %s""",
            (FINISHED_STATUSES, limit),
        )
    ]
    return _fetch(conn, client, ids)


def _fetch(conn: psycopg.Connection, client: Client, ids: list[int]) -> int:
    done = 0
    for start in range(0, len(ids), BATCH):
        batch = ids[start : start + BATCH]
        data = client.graphql(BATCH_QUERY % {"limit": len(batch), "ids": json.dumps(batch)})
        found = data.get("TrdBuy") or []
        for item in found:
            save(conn, item)
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
