"""Резерв госзакупок с единой платформы zakup.gov.kz (открытый API, без ключа).

Госзакупки (системы платформы 1 и 4) есть и в OWS, но платформа показывает объявление сразу,
а OWS — через 10–20 минут, и OWS бывает недоступен часами. Служба `zakup reserve`:

1. Свежие объявления (за последние FRESH) — те, которых у нас ещё нет или нет их лотов:
   карточка и лоты с платформы в виде подробностей OWS (details.save). Номера — номера OWS
   (у платформы это external_id), так что это те же строки: REST и подробности OWS
   потом дописывают своё (файлы, протоколы) — для этого details_fetched_at сбрасываем.
2. Пункты плана (цена за единицу, единица, ЕНС ТРУ, аванс) лотов, у которых в OWS их нет
   (plans_missing): дописываем в подробности OWS, остальное в них не трогаем.

Чего на платформе нет: документов, протоколов, заявок — их по-прежнему даёт только OWS.
Статусы уже известных объявлений обновляет REST OWS (здесь не следим).
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import psycopg

from zakup import db, details
from zakup.goszakup import parse_date
from zakup.skk import Client, PlatformError, _bin, _iso

logger = logging.getLogger(__name__)

SYSTEMS = (1, 4)  # госзакупки на платформе: старая система и новая (с 23.09.2026)
FRESH = timedelta(days=1)
PAGE = 100
# Страниц подряд, где всё уже есть (OWS догнал), — дальше в прошлое не листаем.
CAUGHT_UP_PAGES = 2
PER_RUN = 150  # объявлений за проход (по 2 запроса)
PLANS_PER_RUN = 50
PLANS_AGE = timedelta(days=3)  # пункты плана ищем у опубликованных не раньше
# Неудачная попытка (на платформе нет лотов, ошибка) — не повторяем столько.
RETRY = timedelta(minutes=30)
# Время на свежие объявления за проход: при долгом сбое OWS их много, а самые новые (они первые
# в списке) не должны ждать — остальное доберём на следующем проходе.
PASS_SECONDS = 240
SUBJECTS = {"G": 1, "W": 2, "S": 3}  # товар, работа, услуга — refSubjectTypeId в OWS


def _code(ref: dict | None) -> int | None:
    """Код OWS у справочного значения платформы (egz_code: статус, способ, тип закупки)."""
    value = str((ref or {}).get("egz_code") or "")
    return int(value) if value.isdigit() else None


def _ext(item: dict[str, Any]) -> int | None:
    """Номер OWS у строки платформы (external_id), если он есть и это число."""
    value = str(item.get("external_id") or "")
    return int(value) if value.isdigit() else None


def _kato(address: str | None) -> str | None:
    """КАТО организатора из его адреса на платформе: «194020100, none, …» (система 4) или
    «КАЗАХСТАН, 395420100, …» (система 1)."""
    for part in (address or "").split(",")[:3]:
        if len(part := part.strip()) == 9 and part.isdigit():
            return part
    return None


def plans(lot: dict[str, Any]) -> list[dict[str, Any]]:
    """Пункты плана лота платформы в виде Plans подробностей OWS."""
    out = []
    for plan in lot.get("plan_items") or []:
        if plan.get("is_externally_deleted"):
            continue
        unit = plan.get("measure") or {}
        place = plan.get("delivery_address") or {}
        out.append({
            "price": plan.get("unit_price"), "count": plan.get("count"), "amount": plan.get("total_price"),
            "prepayment": plan.get("prepayment"), "supplyDateRu": plan.get("delivery_deadline_ru"),
            "extraDescRu": plan.get("extra_description_ru"), "extraDescKz": plan.get("extra_description_kk"),
            "refEnstruCode": (plan.get("enstru") or {}).get("code"),
            "RefUnits": {"nameRu": unit.get("name_ru") or unit.get("name"), "nameKz": unit.get("name_kk")},
            "PlansKato": [{"refKatoCode": place["code"], "fullDeliveryPlaceNameRu": place.get("full_name_ru"),
                           "fullDeliveryPlaceNameKz": place.get("full_name_kk")}] if place.get("code") else [],
        })
    return out


def to_detail(card: dict[str, Any], lots: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """Карточка и лоты платформы в виде подробностей OWS (details.FIELDS)."""
    aid = int(card["external_id"])
    organizer = card.get("customer") or card.get("organizer") or {}
    org_bin, org_name = _bin(organizer), organizer.get("name_ru") or organizer.get("name")
    method = _code(card.get("purchase_method"))
    status = _code(card.get("status"))
    if status is None:
        # Без статуса закупка выпала бы из этапов и подписок: по сроку — приём идёт или прошёл.
        end = parse_date(_iso(card.get("offer_end_date")))
        status = 210 if end is None or end > (now or datetime.now(UTC)) else 250
        logger.info("reserve %s: status without OWS code %s", aid, card.get("status"))
    out_lots, years = [], set()
    for lot in lots:
        if not lot.get("external_id"):
            continue  # лота нет в OWS — номер не сопоставить
        items = [p for p in lot.get("plan_items") or [] if not p.get("is_externally_deleted")]
        first = items[0] if items else {}
        years.update(y for p in items if (y := _code(p.get("year"))))
        customer = first.get("organization") or {}
        out_lots.append({
            "id": int(lot["external_id"]), "lotNumber": lot.get("lot_number"),
            "nameRu": first.get("name_ru") or lot.get("name"), "nameKz": first.get("name_kk") or lot.get("name"),
            "descriptionRu": first.get("description_ru") or lot.get("description"),
            "descriptionKz": first.get("description_kk"),
            "amount": lot.get("total_price"), "count": sum(float(p.get("count") or 0) for p in items) or None,
            "trdBuyId": aid, "trdBuyNumberAnno": card.get("announcement_number"),
            "refLotStatusId": _code(lot.get("status")) or status, "refTradeMethodsId": method,
            "customerBin": _bin(customer) or org_bin,
            "customerNameRu": customer.get("name_ru") or customer.get("name") or org_name,
            "plnPointKatoList": [], "isDeleted": 1 if lot.get("is_externally_deleted") else 0,
            "RefLotsStatus": {"nameRu": (lot.get("status") or {}).get("name"), "nameKz": None},
            "Files": [], "Plans": plans(lot),
        })
    kato = _kato(card.get("organizer_address_ru"))
    subject = (card.get("purchase_subject") or {}).get("egz_code")
    return {
        "id": aid, "source": "platform", "platformId": card["id"], "numberAnno": card.get("announcement_number"),
        "nameRu": card.get("name_ru") or card.get("name"), "nameKz": card.get("name_kk") or card.get("name"),
        "totalSum": card.get("total_price"), "countLots": card.get("lot_count"),
        "publishDate": _iso(card.get("publish_date")), "startDate": _iso(card.get("offer_start_date")),
        "endDate": _iso(card.get("offer_end_date")),
        "refBuyStatusId": status, "refTradeMethodsId": method,
        # Тип закупки (первая, повторная) у систем платформы закодирован по-разному — ждём OWS.
        "refTypeTradeId": None,
        "refSubjectTypeId": SUBJECTS.get(subject), "disablePersonId": 1 if card.get("is_among_disabled") else 0,
        # Место поставки — у пунктов плана; нет их — регион организатора (OWS потом уточнит).
        "kato": [kato] if kato else [], "finYear": sorted(years),
        "customerBin": org_bin, "customerNameRu": org_name, "orgBin": org_bin, "orgNameRu": org_name,
        "RefBuyStatus": {"nameRu": (card.get("status") or {}).get("name"), "nameKz": None},
        "RefTradeMethods": {"nameRu": (card.get("purchase_method") or {}).get("name_ru"),
                            "nameKz": (card.get("purchase_method") or {}).get("name_kk")},
        "Organizer": {"bin": org_bin, "nameRu": org_name},
        "Files": [], "Lots": out_lots,
    }


def _rest(detail: dict[str, Any]) -> dict[str, Any]:
    """Поля списка (как REST OWS) для db.upsert_announcement."""
    return {"id": detail["id"], "number_anno": detail["numberAnno"], "name_ru": detail["nameRu"],
            "name_kz": detail["nameKz"], "total_sum": detail["totalSum"], "count_lots": detail["countLots"],
            "ref_trade_methods_id": detail["refTradeMethodsId"], "ref_buy_status_id": detail["refBuyStatusId"],
            "customer_bin": detail["customerBin"], "customer_name_ru": detail["customerNameRu"],
            "org_bin": detail["orgBin"], "org_name_ru": detail["orgNameRu"],
            "publish_date": detail["publishDate"], "start_date": detail["startDate"], "end_date": detail["endDate"],
            "source": "platform"}


def save(conn: psycopg.Connection, card: dict[str, Any], lots: list[dict[str, Any]]) -> bool:
    """Сохраняет объявление с платформы, если подробностей OWS с лотами у нас ещё нет.
    False — нечего сохранять (лотов нет) или OWS успел раньше."""
    detail = to_detail(card, lots)
    if not any(not lot["isDeleted"] for lot in detail["Lots"]):
        return False
    row = conn.execute("SELECT lots_ready FROM announcements WHERE id = %s FOR UPDATE",
                       (detail["id"],)).fetchone()
    if row and row[0]:
        return False  # подробности OWS уже пришли — они полнее
    if row is None:  # строку REST OWS (если уже есть) не переписываем — она главная
        db.upsert_announcement(conn, _rest(detail))
    for lot in detail["Lots"]:
        db.upsert_lot(conn, {"id": lot["id"], "trd_buy_id": detail["id"], "trd_buy_number_anno": detail["numberAnno"],
                             "lot_number": lot["lotNumber"], "name_ru": lot["nameRu"], "name_kz": lot["nameKz"],
                             "description_ru": lot["descriptionRu"], "amount": lot["amount"], "count": lot["count"],
                             "ref_lot_status_id": lot["refLotStatusId"], "ref_trade_methods_id": lot["refTradeMethodsId"],
                             "customer_bin": lot["customerBin"], "customer_name_ru": lot["customerNameRu"],
                             "source": "platform"}, overwrite=False)
    details.save(conn, detail)
    # Подробности OWS (файлы, протоколы) — на ближайшем круге: details.refresh берёт
    # непрочитанные первыми. details_update_date пуст — его пункты плана не затрут пустыми.
    conn.execute("UPDATE announcements SET details_fetched_at = NULL, details_update_date = NULL WHERE id = %s",
                 (detail["id"],))
    return True


def fill_plans(conn: psycopg.Connection, announcement_id: int, lots: list[dict[str, Any]]) -> int:
    """Дописывает пункты плана с платформы лотам подробностей OWS, у которых их нет.
    Возвращает, скольким лотам дописали."""
    row = conn.execute("SELECT detail, details_fetched_at FROM announcements WHERE id = %s FOR UPDATE",
                       (announcement_id,)).fetchone()
    if not row or not row[0]:
        return 0
    detail, fetched_at = row
    found = {int(lot["external_id"]): plans(lot) for lot in lots if lot.get("external_id")}
    filled = 0
    for lot in detail.get("Lots") or []:
        if not lot.get("isDeleted") and not lot.get("Plans") and found.get(lot.get("id")):
            lot["Plans"] = found[lot["id"]]
            filled += 1
    if filled:
        details.save(conn, detail)
        # Это не чтение OWS: его очередь (файлы, протоколы, статусы) не сдвигаем.
        conn.execute("UPDATE announcements SET details_fetched_at = %s WHERE id = %s", (fetched_at, announcement_id))
    return filled


class Reserve:
    """Состояние между проходами: недавние неудачные попытки (чтобы не долбить одно и то же)."""

    def __init__(self, client: Client):
        self.client = client
        self.tried: dict[int, datetime] = {}

    def _skip(self, key: int, now: datetime) -> bool:
        return (when := self.tried.get(key)) is not None and when > now - RETRY

    def _lots(self, platform_id: int) -> list[dict[str, Any]]:
        lots = self.client.get(f"/announcements/{platform_id}/lots/")
        return lots if isinstance(lots, list) else lots.get("results") or []

    def _fresh(self, conn: psycopg.Connection, now: datetime) -> list[dict[str, Any]]:
        """Строки списка платформы, которых у нас нет или нет их лотов, — новые первыми."""
        todo = []
        for system in SYSTEMS:
            offset = quiet = 0
            while offset < 10_000 and quiet < CAUGHT_UP_PAGES:
                try:
                    page = self.client.get("/announcements/", {"system_id": system, "limit": PAGE,
                                                               "offset": offset, "ordering": "-publish_date"})
                except PlatformError as error:
                    # Список одной системы не отдаётся — другую и пункты плана всё равно делаем.
                    logger.warning("reserve list %s: %s", system, error)
                    break
                rows = page.get("results") or [] if isinstance(page, dict) else []
                items = [i for i in rows if isinstance(i, dict) and _ext(i)]
                ready = {r[0] for r in conn.execute(
                    "SELECT id FROM announcements WHERE id = ANY(%s) AND lots_ready", ([_ext(i) for i in items],))}
                conn.commit()
                new = [i for i in items if _ext(i) not in ready and not self._skip(_ext(i), now)
                       and (parse_date(i.get("publish_date")) or now) > now - FRESH]
                todo += new
                quiet = 0 if new else quiet + 1
                offset += PAGE
                oldest = parse_date((items or [{}])[-1].get("publish_date"))
                if not page.get("next") or (oldest and oldest < now - FRESH):
                    break
        todo.sort(key=lambda i: i.get("publish_date") or "", reverse=True)
        return todo

    def run(self, conn: psycopg.Connection, now: datetime | None = None,
            limit: int = PER_RUN, plans_limit: int = PLANS_PER_RUN) -> dict[str, int]:
        now = now or datetime.now(UTC)
        stats = {"listed": 0, "saved": 0, "plans": 0, "failed": 0}
        todo = self._fresh(conn, now)
        stats["listed"] = len(todo)
        deadline = time.monotonic() + PASS_SECONDS
        for item in todo[:limit]:
            if time.monotonic() > deadline:
                break
            aid = _ext(item)
            try:
                card = self.client.get(f"/announcements/{item['id']}/")
                lots = self._lots(item["id"])
            except PlatformError as error:
                logger.warning("reserve %s: %s", aid, error)
                self.tried[aid], stats["failed"] = now, stats["failed"] + 1
                continue
            result = self._save(conn, aid, partial(save, conn, card, lots))
            if result:
                stats["saved"] += 1
            else:
                self.tried[aid] = now
                stats["failed"] += result is None  # None — данные не сохранились (ошибка)
        stats["plans"] = self._plans(conn, now, plans_limit, stats)
        # Память попыток — только за RETRY.
        self.tried = {k: v for k, v in self.tried.items() if v > now - RETRY}
        return stats

    def _save(self, conn: psycopg.Connection, key: int, action) -> Any:
        """Одно объявление — одна транзакция. Встречную блокировку с worker (DeadlockDetected)
        повторяем один раз, странные данные — пропускаем (до следующей попытки)."""
        for attempt in range(2):
            try:
                result = action()
                conn.commit()
                return result
            except psycopg.errors.DeadlockDetected:
                conn.rollback()
                if attempt:
                    raise
            except (psycopg.DataError, psycopg.IntegrityError, KeyError, TypeError, ValueError,
                    AttributeError) as error:
                conn.rollback()
                logger.warning("reserve %s not saved: %r", key, error)
                return None
        return None

    def _plans(self, conn: psycopg.Connection, now: datetime, limit: int, stats: dict[str, int]) -> int:
        """Лоты без пунктов плана в OWS: ищем объявление на платформе по номеру, берём пункты."""
        tried = [-k for k, v in self.tried.items() if k < 0 and v > now - RETRY]
        rows = conn.execute(
            """SELECT id, number_anno FROM announcements
               WHERE plans_missing AND system = 1 AND end_date > %s AND publish_date > %s
                 AND number_anno IS NOT NULL AND NOT id = ANY(%s)
               ORDER BY publish_date DESC LIMIT %s""",
            (now, now - PLANS_AGE, tried, limit)).fetchall()
        conn.commit()
        filled = 0
        for aid, number in rows:
            self.tried[-aid] = now  # ключ со знаком минус — отдельно от свежих объявлений
            try:
                found = []
                for system in SYSTEMS:
                    page = self.client.get("/announcements/", {"system_id": system, "announcement_number": number})
                    found = [i for i in page.get("results") or [] if _ext(i) == aid]
                    if found:
                        break
                if not found:
                    continue
                lots = self._lots(found[0]["id"])
            except PlatformError as error:
                logger.warning("reserve plans %s: %s", aid, error)
                stats["failed"] += 1
                continue
            result = self._save(conn, aid, partial(fill_plans, conn, aid, lots))
            filled += result or 0
            stats["failed"] += result is None
        return filled


def loop(database_url: str, pause: float = 120, once: bool = False) -> int:
    client = Client()
    reserve = Reserve(client)
    try:
        while True:
            started = time.monotonic()
            try:
                with db.connect(database_url) as conn:
                    print(f"reserve: {reserve.run(conn)}", flush=True)
            except (PlatformError, psycopg.OperationalError, KeyError, TypeError, ValueError,
                    AttributeError) as error:
                # Платформа, база или странный ответ — не падаем (память попыток сохраняется).
                logger.warning("reserve: %r", error)
                if once:
                    return 1
            if once:
                return 0
            time.sleep(max(30, pause - (time.monotonic() - started)))
    finally:
        client.close()
