"""Закупки Самрук-Казыны с единой платформы zakup.gov.kz (открытый API, без ключа).

Разведка — docs/ZAKUP_GOV_KZ.md. В OWS этих закупок нет. Кладём их в те же таблицы, что и
госзакупки (system = 3), в том же виде, что подробности из OWS (details.save): поиск,
фильтры, подписки, избранное и страницы работают одинаково. Номера — номер платформы +
ID_OFFSET, с номерами OWS не пересекаются. Службы, которые ходят в OWS по номеру, берут
только system = 1.

Проход: список объявлений с концом приёма за последние WINDOW дней (открытые и недавно
закрытые — у них меняется статус), подробности и лоты — только у новых и изменившихся,
сначала открытые. Названия заказчиков, которых нет на платформе, — из реестра юрлиц
data.egov.kz (zakup.legal_entities).
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg

from zakup import db, details, legal_entities, matcher
from zakup.goszakup import parse_date

logger = logging.getLogger(__name__)

BASE_URL = "https://zakup.gov.kz/api/core/api/public"
# Страница объявления на платформе (по её номеру, не по нашему).
PORTAL_URL = "https://zakup.gov.kz/announcement/{}"
SYSTEM = 3
ID_OFFSET = 10**12
WINDOW = timedelta(days=30)
PAGE = 100
DETAILS_PER_RUN = 300
# Статусы платформы → статусы OWS, по которым работают этапы и фильтры (filters.STAGES).
ANNOUNCEMENT_STATUS = {6: 210, 8: 210, 64: 210, 74: 210, 76: 210,  # приём заявок, торги
                       10: 250, 11: 250, 80: 250,  # рассмотрение, подведение итогов
                       16: 350, 19: 410}  # завершено, отменено
LOT_STATUS = {6: 240, 63: 240, 10: 250, 62: 250, 16: 360, 41: 370, 20: 410}
# Способы Самрук-Казыны — свои номера (10 000 + номер платформы): у них свои правила
# закупок, обеспечения и демпинг по Закону о госзакупках к ним не относятся.
METHOD_OFFSET = 10_000
SUBJECTS = {"G": 1, "W": 2, "S": 3}  # товар, работа, услуга — как refSubjectTypeId в OWS
# Поля списка, которые следим: изменились — перечитываем карточку и лоты.
WATCHED = ("status_id", "offer_end_date", "offer_start_date", "total_price", "lot_count",
           "results_publication_date", "name", "purchase_method_id")
NAMES_BATCH = 30  # объявлений на один запрос названий (у объявления бывает несколько БИН)
# Без изменений в списке всё равно перечитываем раз в столько: статус отдельного лота
# (не состоялся и т. п.) в списке объявлений не виден.
REREAD = timedelta(days=1)
# Итоги приходят и позже WINDOW: «на рассмотрении» старше окна дочитываем по одному.
OLD_PER_RUN = 20
FINAL = {16, 19}  # завершено, отменено — перечитываем, только если изменились в списке
# Когда сборщик начал работать (app_settings): объявления, опубликованные раньше, — не
# «новые» для подписок, иначе первый сбор прислал бы подписчикам пачку из ~2 тыс. закупок.
STARTED_KEY = "skk_started"


class PlatformError(Exception):
    pass


class Client:
    """Открытый API платформы. Вежливо: не чаще запроса в delay секунд. Часть подключений
    с сервера зависает (как у OWS) — повторяем с коротким таймаутом подключения."""

    def __init__(self, delay: float = 1.0, attempts: int = 6, base_url: str = BASE_URL):
        self._delay, self._attempts, self._last = delay, attempts, 0.0
        self._http = httpx.Client(base_url=base_url, timeout=httpx.Timeout(60, connect=5),
                                  headers={"Accept": "application/json"})

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(self._attempts):
            wait = self._delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as error:
                logger.info("platform %s: %s, retry %d", path, type(error).__name__, attempt)
                time.sleep(min(2 + attempt * 5, 30))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                logger.warning("platform %s: HTTP %d, retry %d", path, response.status_code, attempt)
                time.sleep(min(5 * 2**attempt, 120))
                continue
            if response.status_code != 200:
                raise PlatformError(f"{path}: HTTP {response.status_code} {response.text[:200]}")
            return response.json()
        raise PlatformError(f"{path}: no successful response after retries")


def _iso(value: Any) -> str | None:
    """Дата платформы: ISO-строка в списке, unix-время в карточке."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat()
    return str(value)


def announcement_id(platform_id: int) -> int:
    return ID_OFFSET + int(platform_id)


def _bin(org: dict | None) -> str | None:
    return ((org or {}).get("iin_bin") or "").strip() or None


def _org_name(org: dict | None) -> str | None:
    org = org or {}
    return org.get("name_ru") or org.get("full_name_ru") or org.get("name") or None


def bins(card: dict[str, Any], lots: list[dict[str, Any]]) -> list[str]:
    """БИН заказчиков и организатора объявления — для названий из реестра юрлиц."""
    found = [_bin(card.get("customer")), _bin(card.get("organizer"))]
    found += [_bin(plan.get("organization")) for lot in lots for plan in lot.get("plan_items") or []]
    return [b for b in found if b]


def to_detail(item: dict[str, Any], card: dict[str, Any], lots: list[dict[str, Any]],
              names: dict[str, str] | None = None) -> dict[str, Any]:
    """Объявление платформы (item — из списка, card — карточка, lots — её лоты) в виде
    подробностей OWS (details.FIELDS): так его разбирает details.save и показывают страницы.
    names — названия по БИН из реестра юрлиц (на платформе у многих заказчиков их нет)."""
    names = names or {}
    aid = announcement_id(item["id"])
    customer, organizer = card.get("customer") or {}, card.get("organizer") or {}
    customer_bin = _bin(customer) or _bin(organizer)
    method = item.get("purchase_method_id")
    method_id = METHOD_OFFSET + method if method else None
    status_id = ANNOUNCEMENT_STATUS.get(item.get("status_id"))
    if status_id is None:
        # Новый для нас статус платформы: без статуса закупка выпала бы из этапов и подписок.
        # По сроку: приём ещё идёт — «опубликовано», прошёл — «рассмотрение».
        end = parse_date(_iso(item.get("offer_end_date")))
        status_id = 210 if end is None or end > datetime.now(UTC) else 250
        logger.info("skk %s: unknown status %s", item.get("id"), item.get("status_id"))
    subjects, out_lots = set(), []
    for lot in lots:
        plans = []
        lot_bin = None
        for plan in lot.get("plan_items") or []:
            enstru = plan.get("enstru") or {}
            unit = plan.get("measure") or {}
            place = plan.get("delivery_address") or {}
            subject = (plan.get("purchase_subject") or {}).get("code")
            if subject in SUBJECTS:
                subjects.add(SUBJECTS[subject])
            lot_bin = lot_bin or _bin(plan.get("organization"))
            plans.append({
                "price": plan.get("unit_price"), "count": plan.get("count"), "amount": plan.get("total_price"),
                "prepayment": plan.get("prepayment"),
                "supplyDateRu": plan.get("delivery_deadline_ru"),
                "extraDescRu": plan.get("description_ru") or plan.get("extra_description_ru"),
                "extraDescKz": plan.get("description_kk") or plan.get("extra_description_kk"),
                "refEnstruCode": enstru.get("code"),
                "RefUnits": {"nameRu": unit.get("name_ru") or unit.get("name"), "nameKz": unit.get("name_kk")},
                "PlansKato": [{"refKatoCode": place["code"], "fullDeliveryPlaceNameRu": place.get("full_name_ru"),
                               "fullDeliveryPlaceNameKz": place.get("full_name_kk")}] if place.get("code") else [],
            })
        lot_status = (lot.get("status") or {}).get("id")
        out_lots.append({
            "id": ID_OFFSET + int(lot["id"]), "lotNumber": lot.get("lot_number"),
            "nameRu": lot.get("name"), "nameKz": lot.get("name"),
            "descriptionRu": lot.get("description"), "descriptionKz": None,
            "amount": lot.get("total_price"), "count": sum(float(p["count"] or 0) for p in plans) or None,
            "trdBuyId": aid, "trdBuyNumberAnno": item.get("announcement_number"),
            # Неизвестный статус лота — по объявлению (у открытого — «приём ценовых предложений»).
            "refLotStatusId": LOT_STATUS.get(lot_status) or (240 if status_id == 210 else status_id),
            "refTradeMethodsId": method_id,
            "customerBin": lot_bin or customer_bin,
            "customerNameRu": names.get(lot_bin or customer_bin) or _org_name(customer),
            "plnPointKatoList": [], "isDeleted": bool(lot.get("is_externally_deleted")),
            "RefLotsStatus": {"nameRu": (lot.get("status") or {}).get("name"), "nameKz": None},
            "Files": [], "Plans": plans,
        })
    title = item.get("search_name") or {}
    return {
        "id": aid, "system": SYSTEM, "platformId": item["id"], "numberAnno": item.get("announcement_number"),
        "nameRu": title.get("name_ru") or item.get("name"), "nameKz": title.get("name_kk") or card.get("name_kk"),
        "totalSum": item.get("total_price"), "countLots": item.get("lot_count"),
        "publishDate": _iso(item.get("publish_date")), "startDate": _iso(item.get("offer_start_date")),
        "endDate": _iso(item.get("offer_end_date")), "lastUpdateDate": _iso(card.get("timestamp")),
        "refBuyStatusId": status_id, "refTradeMethodsId": method_id,
        "refSubjectTypeId": min(subjects) if len(subjects) == 1 else None,
        "kato": [], "finYear": [(card.get("year") or {}).get("year")] if card.get("year") else [],
        "customerBin": customer_bin,
        "customerNameRu": _org_name(customer) or names.get(customer_bin) or item.get("organizer_name"),
        "orgBin": _bin(organizer) or customer_bin,
        "orgNameRu": item.get("organizer_name") or _org_name(organizer) or names.get(_bin(organizer) or customer_bin),
        "RefBuyStatus": {"nameRu": item.get("status_name"), "nameKz": None},
        "RefTradeMethods": {"nameRu": item.get("purchase_method_name"),
                            "nameKz": (card.get("purchase_method") or {}).get("name_kk")},
        "Organizer": {"bin": _bin(organizer) or customer_bin,
                      "nameRu": item.get("organizer_name") or names.get(_bin(organizer) or customer_bin)},
        "Files": [], "Lots": out_lots,
    }


def _rest(detail: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Поля списка (как REST OWS) для db.upsert_announcement; list — что следим в списке
    платформы: изменилось — перечитываем карточку и лоты."""
    return {"id": detail["id"], "number_anno": detail["numberAnno"], "name_ru": detail["nameRu"],
            "name_kz": detail["nameKz"], "total_sum": detail["totalSum"], "count_lots": detail["countLots"],
            "ref_trade_methods_id": detail["refTradeMethodsId"], "ref_buy_status_id": detail["refBuyStatusId"],
            "customer_bin": detail["customerBin"], "customer_name_ru": detail["customerNameRu"],
            "org_bin": detail["orgBin"], "org_name_ru": detail["orgNameRu"],
            "publish_date": detail["publishDate"], "start_date": detail["startDate"], "end_date": detail["endDate"],
            "last_update_date": detail["lastUpdateDate"], "system": SYSTEM, "platform_id": detail["platformId"],
            "list": {k: item.get(k) for k in WATCHED}}


def started(conn: psycopg.Connection, now: datetime) -> datetime:
    """Время первого прохода сборщика; при первом вызове запоминает now."""
    conn.execute("INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                 (STARTED_KEY, now.isoformat()))
    value = conn.execute("SELECT value FROM app_settings WHERE key = %s", (STARTED_KEY,)).fetchone()[0]
    conn.commit()
    return datetime.fromisoformat(value)


def save(conn: psycopg.Connection, item: dict[str, Any], card: dict[str, Any], lots: list[dict[str, Any]],
         names: dict[str, str] | None = None, since: datetime | None = None) -> None:
    """since — время запуска сборщика: лоты объявлений, опубликованных раньше, сразу
    «увиденные» для подписок (в той же транзакции, как у догрузки истории)."""
    detail = to_detail(item, card, lots, names)
    db.upsert_announcement(conn, _rest(detail, item))
    for lot in detail["Lots"]:
        db.upsert_lot(conn, {"id": lot["id"], "trd_buy_id": detail["id"], "trd_buy_number_anno": detail["numberAnno"],
                             "lot_number": lot["lotNumber"], "name_ru": lot["nameRu"], "name_kz": lot["nameKz"],
                             "description_ru": lot["descriptionRu"], "amount": lot["amount"], "count": lot["count"],
                             "ref_lot_status_id": lot["refLotStatusId"], "ref_trade_methods_id": lot["refTradeMethodsId"],
                             "customer_bin": lot["customerBin"], "customer_name_ru": lot["customerNameRu"],
                             "last_update_date": detail["lastUpdateDate"], "system": SYSTEM})
    # Строки только что могли завестись со значением по умолчанию (госзакупки) — помечаем
    # до сохранения подробностей, в той же транзакции.
    conn.execute("UPDATE announcements SET system = %s WHERE id = %s AND system <> %s",
                 (SYSTEM, detail["id"], SYSTEM))
    conn.execute("UPDATE lots SET system = %s WHERE trd_buy_id = %s AND system <> %s",
                 (SYSTEM, detail["id"], SYSTEM))
    details.save(conn, detail)
    published = parse_date(detail["publishDate"])
    if since is not None and published is not None and published < since:
        matcher.mark_seen(conn, [lot["id"] for lot in detail["Lots"]])


def _listing(client: Client, since: datetime) -> list[dict[str, Any]]:
    """Объявления с концом приёма не раньше since, новые первыми. offset у платформы — до 10 000."""
    items, offset = [], 0
    while offset < 10_000:
        page = client.get("/announcements/", {"system_id": SYSTEM, "limit": PAGE, "offset": offset,
                                               "ordering": "-publish_date",
                                               "offer_end_date__gte": since.strftime("%Y-%m-%dT%H:%M:%S")})
        items += page.get("results") or []
        offset += PAGE
        if not page.get("next"):
            break
    return items


def _old_reviews(conn: psycopg.Connection, client: Client, now: datetime) -> list[dict[str, Any]]:
    """Объявления «на рассмотрении» с концом приёма раньше окна: итог (завершено, отменено)
    платформа ставит и через месяцы. Строки списка — по номеру объявления, давно не
    проверенные первыми."""
    numbers = [row[0] for row in conn.execute(
        """SELECT number_anno FROM announcements
           WHERE system = %s AND status_id = 250 AND end_date < %s AND number_anno IS NOT NULL
           ORDER BY details_fetched_at NULLS FIRST LIMIT %s""",
        (SYSTEM, now - WINDOW, OLD_PER_RUN))]
    conn.commit()
    items = []
    for number in numbers:
        try:
            page = client.get("/announcements/", {"system_id": SYSTEM, "announcement_number": number})
        except PlatformError as error:
            logger.warning("skk %s: %s", number, error)
            continue
        items += [i for i in page.get("results") or [] if i.get("announcement_number") == number]
    return items


def run(conn: psycopg.Connection, client: Client, now: datetime | None = None,
        limit: int = DETAILS_PER_RUN, http: httpx.Client | None = None) -> dict[str, int]:
    """Один проход. http — клиент для реестра юрлиц (один на проход и дольше)."""
    now = now or datetime.now(UTC)
    since = started(conn, now)
    items = _listing(client, now - WINDOW)
    known = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT id, raw, details_fetched_at FROM announcements WHERE system = %s AND id = ANY(%s)",
        (SYSTEM, [announcement_id(i["id"]) for i in items]))}
    conn.commit()  # платформу ждём без открытой транзакции

    def changed(item: dict) -> bool:
        raw, fetched_at = known.get(announcement_id(item["id"]), (None, None))
        # Из списка в raw — только то, что меняется: статус, сроки, сумма, число лотов.
        return (raw is None or fetched_at is None
                or (fetched_at < now - REREAD and item.get("status_id") not in FINAL)
                or any((raw.get("list") or {}).get(k) != item.get(k) for k in WATCHED))

    todo = [i for i in items if changed(i)]
    # Сначала открытые (по ним ещё подают заявки), из них — новые; давние «на рассмотрении» — в конце.
    todo.sort(key=lambda i: (i.get("offer_end_date") or "") < now.isoformat())
    todo += _old_reviews(conn, client, now)
    saved = failed = 0
    own = http is None
    http = http or httpx.Client(timeout=httpx.Timeout(60, connect=5))
    try:
        # Пачками: карточки и лоты пачки, затем одним запросом названия её заказчиков.
        for start in range(0, min(len(todo), limit), NAMES_BATCH):
            fetched = []
            for item in todo[start:min(start + NAMES_BATCH, limit)]:
                try:
                    card = client.get(f"/announcements/{item['id']}/")
                    lots = client.get(f"/announcements/{item['id']}/lots/")
                except PlatformError as error:
                    logger.warning("skk %s: %s", item["id"], error)
                    failed += 1
                    continue
                fetched.append((item, card, lots if isinstance(lots, list) else lots.get("results") or []))
            wanted = [b for _, card, lots in fetched for b in bins(card, lots)]
            legal_entities.ensure(conn, wanted, http)
            known_names = legal_entities.names(conn, wanted)
            conn.commit()
            for item, card, lots in fetched:
                try:
                    save(conn, item, card, lots, known_names, since)
                    conn.commit()
                    saved += 1
                except (psycopg.DataError, psycopg.IntegrityError, KeyError, TypeError, ValueError,
                        AttributeError) as error:
                    # Одно странное объявление не должно останавливать сборщик: остальные
                    # сохраняем, это — снова на следующем проходе.
                    conn.rollback()
                    logger.warning("skk %s not saved: %r", item.get("id"), error)
                    failed += 1
    finally:
        if own:
            http.close()
    return {"listed": len(items), "changed": len(todo), "saved": saved, "failed": failed}
