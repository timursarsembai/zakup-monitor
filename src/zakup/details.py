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
               detail = %(detail)s, details_fetched_at = now(), lots_ready = %(lots_ready)s,
               -- Когда лоты пришли впервые (справа — прежнее значение lots_ready): замер задержки.
               lots_ready_at = CASE WHEN %(lots_ready)s AND lots_ready_at IS NULL THEN now() ELSE lots_ready_at END,
               details_update_date = coalesce(%(last_update_date)s, last_update_date)
           WHERE id = %(id)s""",
        {**fields, "detail": Jsonb(item), "lots_ready": bool(lots)},
    )
    raw_lots = {int(lot["id"]): lot for lot in item.get("Lots") or []}
    for lot in lots:
        raw = {k: v for k, v in raw_lots[lot["id"]].items() if not k[:1].isupper()}
        # Лот мог ещё не дойти через REST: заводим его отсюда, REST потом дополнит.
        db.upsert_lot(conn, normalize(raw), overwrite=False)
        conn.execute(
            """UPDATE lots SET prepayment = %(prepayment)s, kato = %(kato)s, price = %(price)s,
                   unit_name = %(unit_name)s, extra_desc = %(extra_desc)s,
                   supply_date = %(supply_date)s, enstru = %(enstru)s, spec_url = %(spec_url)s,
                   unit_name_kz = %(unit_name_kz)s, extra_desc_kz = %(extra_desc_kz)s
               WHERE id = %(id)s""",
            lot,
        )
    codes(conn, [int(item["id"])])
    if fields.get("itogi_date"):
        # Дата итогов у разобранных итогов лотов (миграция 0021) — по подробностям объявления.
        conn.execute(
            """UPDATE lot_results r SET result_date = %(date)s FROM lots l
               WHERE l.id = r.lot_id AND l.trd_buy_id = %(id)s AND r.result_date IS DISTINCT FROM %(date)s""",
            {"date": fields["itogi_date"], "id": int(item["id"])})


def codes(conn: psycopg.Connection, announcement_ids: list[int]) -> None:
    """Списки кодов регионов и разделов для фильтров (миграция 0019) — у объявлений и их лотов."""
    # Порядок блокировок как в save: сначала объявления, потом их лоты — иначе встречные
    # save и fill_codes (разные службы) могут заблокировать друг друга.
    conn.execute("UPDATE announcements a SET regions = codes2(a.kato) WHERE a.id = ANY(%(ids)s)",
                 {"ids": announcement_ids})
    conn.execute(
        """UPDATE lots l SET regions = codes2(coalesce(nullif(l.kato, '{}'), a.kato)),
                             divisions = codes2(l.enstru), divisions_guessed = false
           FROM announcements a WHERE a.id = l.trd_buy_id AND l.trd_buy_id = ANY(%(ids)s)""",
        {"ids": announcement_ids})
    guess_divisions(conn, "l.trd_buy_id = ANY(%(ids)s)", {"ids": announcement_ids})
    _announcement_divisions(conn, announcement_ids)


def _announcement_divisions(conn: psycopg.Connection, announcement_ids: list[int]) -> None:
    """Разделы объявления — все разделы его лотов (и определённые по названию)."""
    conn.execute(
        """UPDATE announcements a SET divisions = (SELECT codes2(array_agg(c)) FROM lots l, unnest(l.divisions) c
                                                   WHERE l.trd_buy_id = a.id)
           WHERE a.id = ANY(%(ids)s)""",
        {"ids": announcement_ids})


# Надёжное название: столько лотов с кодом и такая доля их в одном разделе.
NAME_MIN_LOTS = 3
NAME_MIN_SHARE = 0.7


def refresh_name_divisions(conn: psycopg.Connection) -> int:
    """Таблица «название → раздел» по лотам с настоящим кодом ЕНС ТРУ (миграция 0023)."""
    conn.execute("DELETE FROM name_divisions")
    count = conn.execute(
        """INSERT INTO name_divisions (name, division, lots, share)
           SELECT n, d, c, c::real / total FROM (
               SELECT n, d, count(*) AS c, sum(count(*)) OVER (PARTITION BY n) AS total,
                      row_number() OVER (PARTITION BY n ORDER BY count(*) DESC, d) AS rk
               FROM (SELECT lower(btrim(name_ru)) AS n, left(enstru[1], 2) AS d FROM lots
                     WHERE enstru IS NOT NULL AND enstru <> '{}' AND name_ru IS NOT NULL) coded
               GROUP BY n, d) ranked
           WHERE rk = 1 AND total >= %(min_lots)s AND c::real / total >= %(min_share)s""",
        {"min_lots": NAME_MIN_LOTS, "min_share": NAME_MIN_SHARE}).rowcount
    conn.commit()
    return count


def guess_divisions(conn: psycopg.Connection, where: str = "true", params: dict | None = None) -> int:
    """Лотам без кода ЕНС ТРУ — раздел по названию (только изменившиеся строки). where —
    условие на лоты l (alias), например «l.trd_buy_id = ANY(%(ids)s)»."""
    return conn.execute(
        f"""UPDATE lots l SET divisions = ARRAY[nd.division], divisions_guessed = true
            FROM name_divisions nd
            WHERE nd.name = lower(btrim(l.name_ru)) AND (l.enstru IS NULL OR l.enstru = '{{}}')
              AND l.divisions IS DISTINCT FROM ARRAY[nd.division] AND {where}""",
        params or {}).rowcount


def fill_codes(conn: psycopg.Connection, batch: int = 2000) -> int:
    """Старые записи без списков кодов — пачками, с коммитом после каждой: не держать таблицы."""
    done = 0
    while ids := [r[0] for r in conn.execute(
            "SELECT id FROM announcements WHERE regions IS NULL ORDER BY id LIMIT %s", (batch,))]:
        codes(conn, ids)
        conn.commit()
        done += len(ids)
    # Лоты без объявления в базе или добавленные после подробностей — по своим кодам.
    while ids := [r[0] for r in conn.execute(
            "SELECT id FROM lots WHERE regions IS NULL LIMIT %s", (batch,))]:
        conn.execute("""UPDATE lots l SET regions = codes2(coalesce(nullif(l.kato, '{}'),
                            (SELECT a.kato FROM announcements a WHERE a.id = l.trd_buy_id))),
                            divisions = codes2(l.enstru) WHERE l.id = ANY(%s)""", (ids,))
        conn.commit()
        done += len(ids)
    # Разделы по названию: таблицу — заново, лотам без кода — только изменившееся, пачками.
    refresh_name_divisions(conn)
    while ids := [r[0] for r in conn.execute(
            """SELECT l.id FROM lots l JOIN name_divisions nd ON nd.name = lower(btrim(l.name_ru))
               WHERE (l.enstru IS NULL OR l.enstru = '{}') AND l.divisions IS DISTINCT FROM ARRAY[nd.division]
               LIMIT %s""", (batch,))]:
        guess_divisions(conn, "l.id = ANY(%(ids)s)", {"ids": ids})
        # Лоты уже были в базе: раздел появился задним числом — подписки с категорией не
        # должны прислать их как новые.
        from zakup import matcher

        matcher.mark_seen(conn, ids)
        announcements = [r[0] for r in conn.execute(
            "SELECT DISTINCT trd_buy_id FROM lots WHERE id = ANY(%s) AND trd_buy_id IS NOT NULL", (ids,))]
        # Лоты и объявления — разными транзакциями: подробности (save) блокируют их в обратном
        # порядке (объявление, потом лоты), вместе здесь получилась бы встречная блокировка.
        conn.commit()
        _announcement_divisions(conn, announcements)
        conn.commit()
        done += len(ids)
    return done


def fetch_one(conn: psycopg.Connection, client: Client, announcement_id: int) -> bool:
    conn.commit()  # портал ждём без открытой транзакции
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
                      OR details_fetched_at < now() - interval '1 day'
                      -- API портала отдаёт лоты с задержкой (до ~20 минут): пока в подробностях
                      -- нет лотов, перечитываем на каждом круге сборщика (раз в 2 минуты) первые
                      -- 3 часа после публикации, дальше — раз в час (отменённые, без лотов совсем).
                      OR (NOT lots_ready AND details_fetched_at < now() - CASE
                              WHEN coalesce(publish_date, now()) > now() - interval '3 hours' THEN interval '1 minute'
                              ELSE interval '1 hour' END))
               -- Сначала новые и те, где ещё нет лотов: они не видны на сайте.
               ORDER BY details_fetched_at IS NOT NULL, lots_ready, details_fetched_at, end_date
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
            # Условие — как у частичного индекса announcements_finished_incomplete (миграция 0031),
            # статусы — константой: с параметром планировщик не докажет, что индекс подходит.
            f"""SELECT id FROM announcements
               WHERE status_id IN ({", ".join(map(str, FINISHED_STATUSES))})
                 AND (detail IS NULL OR NOT detail ? 'nameKz'
                      OR (detail->>'refBuyStatusId')::int IS DISTINCT FROM status_id)
                 AND (details_fetched_at IS NULL OR details_fetched_at < now() - interval '1 hour')
               ORDER BY end_date DESC
               LIMIT %s""",
            (limit,),
        )
    ]
    return _fetch(conn, client, ids)


def _save_retrying(conn: psycopg.Connection, item: dict[str, Any]) -> None:
    """Одно объявление — одна транзакция: лоты держим недолго. Подробности открытых и
    завершённых (worker и archive) и повторный проход REST пишут в одни лоты; встречную
    блокировку (DeadlockDetected) база снимает, отменив одну сторону, — её повторяем."""
    try:
        save(conn, item)
    except psycopg.errors.DeadlockDetected:
        conn.rollback()
        logger.warning("details of %s: deadlock, retrying", item.get("id"))
        save(conn, item)
    conn.commit()


def _fetch(conn: psycopg.Connection, client: Client, ids: list[int]) -> int:
    conn.commit()  # ждём портал без открытой транзакции (выборка ids выше её открыла)
    done = 0
    for start in range(0, len(ids), BATCH):
        batch = ids[start : start + BATCH]
        data = client.graphql(BATCH_QUERY % {"limit": len(batch), "ids": json.dumps(batch)})
        found = data.get("TrdBuy") or []
        for item in found:
            _save_retrying(conn, item)
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
