"""Резерв лотов со страниц старого портала old.goszakup.gov.kz — пока стоит API.

12–14.11.2026 список лотов OWS застыл, а платформа zakup.gov.kz (reserve) застыла тоже: тысячи
объявлений OWS остались без лотов. Старый портал их показывает: вкладка «Лоты» объявления
(/ru/announce/index/<номер OWS>?tab=lots, по 20 на страницу) и окно лота
(POST /ru/announce/ajax_load_lot/<номер>/ id=<номер лота портала>) — там заказчик, ЕНС ТРУ,
цена, количество, аванс и место поставки с КАТО.

Служба `zakup oldportal`: открытые объявления OWS без лотов, что закрываются раньше, — первыми;
запрос — раз в секунду. Лоты пишем в подробности OWS (как reserve.fill_lots, пометка
source=oldportal) с временными номерами (db.TEMP_LOT_BASE + номер лота портала): номера OWS
страница не даёт. Когда лоты придут из OWS, db.adopt_temp_lot заменит временные настоящими.
Капчи на портале нет; ничего не обходим — только читаем открытые страницы, вежливо.
"""

import html
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg

from zakup import db, details

logger = logging.getLogger(__name__)

BASE_URL = "https://old.goszakup.gov.kz"
PER_RUN = 30  # объявлений за проход
MAX_PAGES = 30  # страниц лотов у объявления (по 20) — дальше не листаем
AFTER = timedelta(minutes=20)  # моложе — дело OWS и платформы
RECHECK = timedelta(hours=1)  # не вышло (лотов нет, ошибка) — снова не раньше
SUBJECT = "oldportal"


class PortalError(Exception):
    pass


class Client:
    """Не чаще запроса в delay секунд. Подключения к порталу то и дело не устанавливаются
    (половина с 14.11.2026) — короткий таймаут подключения и много повторов."""

    def __init__(self, delay: float = 1.0, attempts: int = 8, base_url: str = BASE_URL,
                 transport: httpx.BaseTransport | None = None):
        self._delay, self._attempts, self._last = delay, attempts, 0.0
        self._http = httpx.Client(base_url=base_url, timeout=httpx.Timeout(30, connect=4),
                                  transport=transport, follow_redirects=True,
                                  headers={"User-Agent": "Mozilla/5.0 (compatible; reserve/1.0)"})

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> str:
        for attempt in range(self._attempts):
            wait = self._delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as error:
                logger.info("oldportal %s: %s, retry %d", path, type(error).__name__, attempt)
                continue  # пауза — та же delay перед следующим запросом
            except httpx.HTTPError as error:  # петля переадресаций, битая кодировка…
                raise PortalError(f"{path}: {type(error).__name__}") from error
            if response.status_code == 429 or response.status_code >= 500:
                logger.warning("oldportal %s: HTTP %d, retry %d", path, response.status_code, attempt)
                time.sleep(min(5 * 2**attempt, 60))
                continue
            if response.status_code != 200:
                raise PortalError(f"{path}: HTTP {response.status_code}")
            return response.text
        raise PortalError(f"{path}: no successful response after retries")


_LOT_LINK = re.compile(r"data-lot-id=['\"](\d+)['\"]\s*>\s*([^<]+?)\s*</a>")
_ROW = re.compile(r"<tr>\s*<th>(.*?)</th>\s*<td>(.*?)</td>\s*</tr>", re.S)
_PLACE = re.compile(r"<p>(.*?)</p>", re.S)
_TAGS = re.compile(r"<[^>]+>")


def _text(value: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", value)).split())


def lot_links(page: str) -> list[tuple[int, str]]:
    """Вкладка «Лоты»: (номер лота портала, номер лота «88602578-ЗЦП2») по порядку."""
    return [(int(n), _text(number)) for n, number in _LOT_LINK.findall(page)]


def has_next(page: str, current: int) -> bool:
    """Есть ли ссылка на следующую страницу лотов (порядок параметров — любой)."""
    return re.search(rf"[?&;]page={current + 1}(?!\d)", page) is not None


def _number(value: str | None) -> float | None:
    value = (value or "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def parse_lot(page: str) -> dict[str, Any]:
    """Окно лота: поля по подписям и места поставки [(КАТО, адрес)]."""
    fields, places = {}, []
    for name, value in _ROW.findall(page):
        name = _text(name)
        if name.startswith("Место поставки"):
            for part in _PLACE.findall(value) or [value]:
                address = _text(part.split("<br")[0])
                code = address.split(",")[0].strip()
                if len(code) == 9 and code.isdigit():
                    places.append((code, address[len(code):].strip(" ,") or None))
        else:
            fields[name] = _text(value)
    fields["places"] = places
    return fields


def to_lot(announcement: dict[str, Any], portal_id: int, number: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Лот портала в виде лота подробностей OWS (details.FIELDS) с временным номером."""
    amount = _number(fields.get("Запланированная сумма")) or _number(fields.get("Сумма 1 год"))
    count = _number(fields.get("Количество"))
    unit = fields.get("Единица измерения") or None
    plan = {
        "price": _number(fields.get("Цена за единицу")), "count": count, "amount": amount,
        "prepayment": _number(fields.get("Размер авансового платежа %")),
        "supplyDateRu": fields.get("Срок поставки ТРУ") or None,
        "extraDescRu": fields.get("Дополнительная характеристика") or None, "extraDescKz": None,
        "refEnstruCode": fields.get("Код ТРУ") or None,
        "RefUnits": {"nameRu": unit, "nameKz": None},
        "PlansKato": [{"refKatoCode": code, "fullDeliveryPlaceNameRu": address, "fullDeliveryPlaceNameKz": None}
                      for code, address in fields.get("places") or []],
    }
    return {
        "id": db.TEMP_LOT_BASE + portal_id, "lotNumber": fields.get("Лот №") or number,
        "nameRu": fields.get("Наименование ТРУ") or fields.get("Краткая характеристика"), "nameKz": None,
        "descriptionRu": fields.get("Краткая характеристика") or None, "descriptionKz": None,
        "amount": amount, "count": count, "trdBuyId": announcement["id"],
        "trdBuyNumberAnno": announcement["number_anno"],
        "refLotStatusId": announcement["status_id"], "refTradeMethodsId": announcement["trade_method_id"],
        "customerBin": fields.get("БИН заказчика") or announcement["customer_bin"],
        "customerNameRu": fields.get("Наименование заказчика") or announcement["customer_name_ru"],
        "plnPointKatoList": [], "isDeleted": 0,
        "RefLotsStatus": {"nameRu": fields.get("Статус лота") or None, "nameKz": None},
        "Files": [], "Plans": [plan],
    }


def fetch(client: Client, announcement: dict[str, Any]) -> list[dict[str, Any]]:
    """Все лоты объявления со старого портала (в виде лотов OWS)."""
    aid, links, seen = announcement["id"], [], set()
    for page_no in range(1, MAX_PAGES + 1):
        params = {"tab": "lots"} | ({"page": page_no} if page_no > 1 else {})
        page = client.request("GET", f"/ru/announce/index/{aid}", params=params)
        new = [(n, number) for n, number in lot_links(page) if n not in seen]
        seen.update(n for n, _ in new)
        links += new
        if not new or not has_next(page, page_no):
            break
    else:
        # Не долистали — неполный список лотов не сохраняем (иначе он считался бы полным).
        raise PortalError(f"{aid}: more than {MAX_PAGES} pages of lots")
    lots = []
    for portal_id, number in links:
        fields = parse_lot(client.request("POST", f"/ru/announce/ajax_load_lot/{aid}/", data={"id": portal_id}))
        if not fields.get("Лот №") and not fields.get("Наименование ТРУ"):
            raise PortalError(f"{aid}: lot {portal_id} window without fields")
        lots.append(to_lot(announcement, portal_id, number, fields))
    return lots


def fill(conn: psycopg.Connection, announcement_id: int, lots: list[dict[str, Any]]) -> bool:
    """Лоты портала — в подробности OWS, где лотов нет (как reserve.fill_lots). False — лоты
    уже есть (OWS или платформа успели) или подробностей нет."""
    if not lots:
        return False
    row = conn.execute("SELECT detail, lots_ready FROM announcements WHERE id = %s FOR UPDATE",
                       (announcement_id,)).fetchone()
    if row is None or not row[0] or row[1]:
        return False
    if conn.execute("SELECT 1 FROM lots WHERE trd_buy_id = %s AND id < %s LIMIT 1",
                    (announcement_id, db.TEMP_LOT_BASE)).fetchone():
        return False  # лоты OWS уже в базе — подробности вот-вот их покажут
    detail = row[0]
    detail.update(Lots=lots, source=SUBJECT)
    details.save(conn, detail)  # лоты заводит он же (db.upsert_lot из полей лота)
    # Как reserve.fill_lots: OWS перечитаем на ближайшем круге, а его пустые лоты наши не затрут.
    conn.execute("UPDATE announcements SET details_fetched_at = NULL, details_update_date = NULL WHERE id = %s",
                 (announcement_id,))
    return True


def todo(conn: psycopg.Connection, now: datetime, limit: int) -> list[dict[str, Any]]:
    """Открытые объявления OWS без лотов — что закрывается раньше, первыми."""
    rows = conn.execute(
        """SELECT a.id, a.number_anno, a.status_id, a.trade_method_id, a.customer_bin, a.customer_name_ru
           FROM announcements a
           WHERE NOT a.lots_ready AND a.system = 1 AND a.end_date > %s AND a.publish_date < %s
             AND a.detail IS NOT NULL
             AND (a.oldportal_checked_at IS NULL OR a.oldportal_checked_at < %s)
             AND NOT EXISTS (SELECT 1 FROM lots l WHERE l.trd_buy_id = a.id AND l.id < %s)
           ORDER BY a.end_date LIMIT %s""",
        (now, now - AFTER, now - RECHECK, db.TEMP_LOT_BASE, limit)).fetchall()
    conn.commit()
    keys = ("id", "number_anno", "status_id", "trade_method_id", "customer_bin", "customer_name_ru")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def run(conn: psycopg.Connection, client: Client, now: datetime | None = None,
        limit: int = PER_RUN) -> dict[str, int]:
    now = now or datetime.now(UTC)
    stats = {"todo": 0, "saved": 0, "lots": 0, "empty": 0, "failed": 0}
    items = todo(conn, now, limit)
    stats["todo"] = len(items)
    for item in items:
        try:
            lots = fetch(client, item)
        except (PortalError, KeyError, TypeError, ValueError) as error:  # и странная страница
            logger.warning("oldportal %s: %s", item["id"], error)
            lots, stats["failed"] = None, stats["failed"] + 1
        for _ in range(2):
            try:
                saved = fill(conn, item["id"], lots) if lots else False
                if not saved:
                    conn.execute("UPDATE announcements SET oldportal_checked_at = %s WHERE id = %s",
                                 (now, item["id"]))
                conn.commit()
                break
            except psycopg.errors.DeadlockDetected:
                conn.rollback()  # встречная блокировка с worker — повторяем раз
                saved = False
            except (psycopg.DataError, psycopg.IntegrityError, psycopg.ProgrammingError, psycopg.InternalError,
                    KeyError, TypeError, ValueError, AttributeError) as error:
                conn.rollback()
                logger.warning("oldportal %s not saved: %r", item["id"], error)
                saved, stats["failed"] = False, stats["failed"] + 1
                break
        stats["saved"] += bool(saved)
        stats["lots"] += len(lots) if saved else 0
        stats["empty"] += lots == []
    return stats


def loop(database_url: str, pause: float = 60, once: bool = False) -> int:
    client = Client()
    try:
        while True:
            started = time.monotonic()
            try:
                with db.connect(database_url) as conn:
                    print(f"oldportal: {run(conn, client)}", flush=True)
            except (PortalError, psycopg.Error, httpx.HTTPError) as error:
                logger.warning("oldportal: %r", error)
                if once:
                    return 1
            if once:
                return 0
            time.sleep(max(10, pause - (time.monotonic() - started)))
    finally:
        client.close()
