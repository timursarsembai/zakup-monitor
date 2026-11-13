"""Резерв госзакупок с единой платформы zakup.gov.kz (открытый API, без ключа).

Госзакупки (системы платформы 1 и 4) есть и в OWS, но платформа показывает объявление сразу,
а OWS — через 10–20 минут, и OWS бывает недоступен часами. Служба `zakup reserve`:

1. Свежие объявления (за последние FRESH) — те, которых у нас ещё нет или нет их лотов:
   карточка и лоты с платформы в виде подробностей OWS (details.save). Номера — номера OWS
   (у платформы это external_id), так что это те же строки: REST и подробности OWS
   потом дописывают своё (файлы, протоколы) — для этого details_fetched_at сбрасываем.
2. Пункты плана (цена за единицу, единица, ЕНС ТРУ, аванс) лотов, у которых в OWS их нет
   (plans_missing): дописываем в подробности OWS, остальное в них не трогаем.

3. Документы (general-files): техспецификации лотов и общие документы — в Files подробностей,
   как у OWS (по ним разбор спецификаций берёт файл). Объявлениям с платформы, сохранённым без
   документов, добираем их отдельно (_files), не больше FILES_PER_RUN за проход.

4. Добор лотов (_backlog): объявления из OWS, у которых лотов так и нет (список лотов OWS
   застывает — так было 12–13.11.2026 больше суток), — ищем на платформе по номеру и берём
   карточку с лотами, как в п. 1. Список платформы для этого не листаем: пропуски бывают в
   середине дня, а п. 1 останавливается на верхних страницах, где всё уже есть.

Чего на платформе нет: протоколов, заявок — их по-прежнему даёт только OWS.
Статусы уже известных объявлений обновляет REST OWS (здесь не следим).
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import psycopg

from zakup import db, details, portal, spec
from zakup.goszakup import parse_date
from zakup.skk import Client, PlatformError, _bin, _iso, results

logger = logging.getLogger(__name__)

SYSTEMS = (1, 4)  # госзакупки на платформе: старая система и новая (с 23.09.2026)
FRESH = timedelta(days=1)
PAGE = 100
# Страниц подряд, где всё уже есть (OWS догнал), — дальше в прошлое не листаем.
CAUGHT_UP_PAGES = 2
PER_RUN = 150  # объявлений за проход (по 2 запроса)
PLANS_PER_RUN = 50
LINKS_PER_RUN = 30  # добор номеров платформы открытым объявлениям (ссылка на портал)
LINKS_SECONDS = 90  # …и не дольше стольких секунд сверх основного прохода
FILES_PER_RUN = 30  # добор документов объявлениям с платформы, сохранённым без них
BACKLOG_PER_RUN = 100  # добор лотов объявлениям OWS без лотов
BACKLOG_SECONDS = 120  # …и не дольше стольких секунд сверх времени свежих (не занятое ими — тоже его)
BACKLOG_AFTER = timedelta(minutes=20)  # моложе — дело п. 1 (свежие) и OWS
BACKLOG_AGE = timedelta(days=7)  # старше — не ищем (в OWS лоты давно должны быть)
# Не нашли на платформе — снова через час, не через сутки (как у ссылок): 13.11.2026 платформа
# тоже застыла, а когда оживёт, лоты нужны сразу.
BACKLOG_RECHECK = timedelta(hours=1)
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


_ext = portal.ext  # номер OWS у строки платформы


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


def files(groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """Документы платформы (general-files) в виде Files OWS: (общие, по номеру лота OWS).
    Название — название группы («Техническая спецификация»…): по нему spec.spec_file находит файл.
    Странные строки пропускаем: из-за документов объявление не должно остаться несохранённым."""
    common: list[dict[str, Any]] = []
    by_lot: dict[int, list[dict[str, Any]]] = {}
    for group in groups:
        meta = group.get("group")
        name = meta.get("name") if isinstance(meta, dict) else None
        entries = group.get("files")
        if not isinstance(name, str) or not name or not isinstance(entries, list):
            continue
        for entry in entries:
            file = entry.get("file") if isinstance(entry, dict) else None
            url = file.get("file_url") if isinstance(file, dict) else None
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            # Номер со знаком «p»: не совпадёт с номером файла OWS (страница убирает повторы по нему).
            out = {"id": f"p{entry.get('id') or file.get('external_id') or url}", "nameRu": name, "nameKz": None,
                   "originalName": file.get("filename"), "filePath": url}
            lot = entry.get("lot")
            lot = str(lot.get("external_id") or "") if isinstance(lot, dict) else ""
            if lot.isdigit():
                by_lot.setdefault(int(lot), []).append(out)
            else:
                common.append(out)
    return common, by_lot


def add_files(detail: dict[str, Any], groups: list[dict[str, Any]]) -> None:
    """Документы платформы — в подробности (общие объявлению, остальные — своим лотам).
    Отметка platformFiles (добирать не нужно) — только если документы нашлись: пустой ответ
    бывает и при сбое, такие спросим ещё раз (_files)."""
    common, by_lot = files(groups)
    active = [lot for lot in detail.get("Lots") or [] if not lot.get("isDeleted")]
    if len(active) == 1 and not by_lot.get(active[0].get("id")):
        # Один лот, а спецификация без привязки к лоту — это его спецификация.
        own = [f for f in common if "специфик" in f["nameRu"].lower()]
        by_lot[active[0].get("id")] = own
        common = [f for f in common if f not in own]
    detail["Files"] = common
    for lot in detail.get("Lots") or []:
        lot["Files"] = by_lot.get(lot.get("id"), [])
    if common or any(by_lot.values()):
        detail["platformFiles"] = True


def to_detail(card: dict[str, Any], lots: list[dict[str, Any]], now: datetime | None = None,
              groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Карточка и лоты платформы в виде подробностей OWS (details.FIELDS); groups — документы
    (general-files), None — не получены (доберём потом)."""
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
    detail = {
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
    if groups is not None:
        add_files(detail, groups)
    return detail


def _rest(detail: dict[str, Any]) -> dict[str, Any]:
    """Поля списка (как REST OWS) для db.upsert_announcement."""
    return {"id": detail["id"], "number_anno": detail["numberAnno"], "name_ru": detail["nameRu"],
            "name_kz": detail["nameKz"], "total_sum": detail["totalSum"], "count_lots": detail["countLots"],
            "ref_trade_methods_id": detail["refTradeMethodsId"], "ref_buy_status_id": detail["refBuyStatusId"],
            "customer_bin": detail["customerBin"], "customer_name_ru": detail["customerNameRu"],
            "org_bin": detail["orgBin"], "org_name_ru": detail["orgNameRu"],
            "publish_date": detail["publishDate"], "start_date": detail["startDate"], "end_date": detail["endDate"],
            "source": "platform"}


def save(conn: psycopg.Connection, card: dict[str, Any], lots: list[dict[str, Any]],
         groups: list[dict[str, Any]] | None = None) -> bool:
    """Сохраняет объявление с платформы, если подробностей OWS с лотами у нас ещё нет.
    False — нечего сохранять (лотов нет) или OWS успел раньше."""
    detail = to_detail(card, lots, groups=groups)
    if not any(not lot["isDeleted"] for lot in detail["Lots"]):
        return False
    row = conn.execute("SELECT lots_ready FROM announcements WHERE id = %s FOR UPDATE",
                       (detail["id"],)).fetchone()
    if row and row[0]:
        return False  # подробности OWS уже пришли — они полнее
    if row is None:  # строку REST OWS (если уже есть) не переписываем — она главная
        db.upsert_announcement(conn, _rest(detail))
    _upsert_lots(conn, detail)
    details.save(conn, detail)
    portal.remember(conn, [card])
    # Подробности OWS (файлы, протоколы) — на ближайшем круге: details.refresh берёт
    # непрочитанные первыми. details_update_date пуст — его пункты плана не затрут пустыми.
    conn.execute("UPDATE announcements SET details_fetched_at = NULL, details_update_date = NULL WHERE id = %s",
                 (detail["id"],))
    return True


def _upsert_lots(conn: psycopg.Connection, detail: dict[str, Any]) -> None:
    for lot in detail["Lots"]:
        db.upsert_lot(conn, {"id": lot["id"], "trd_buy_id": detail["id"], "trd_buy_number_anno": detail["numberAnno"],
                             "lot_number": lot["lotNumber"], "name_ru": lot["nameRu"], "name_kz": lot["nameKz"],
                             "description_ru": lot["descriptionRu"], "amount": lot["amount"], "count": lot["count"],
                             "ref_lot_status_id": lot["refLotStatusId"], "ref_trade_methods_id": lot["refTradeMethodsId"],
                             "customer_bin": lot["customerBin"], "customer_name_ru": lot["customerNameRu"],
                             "source": "platform"}, overwrite=False)


def fill_lots(conn: psycopg.Connection, card: dict[str, Any], lots: list[dict[str, Any]],
              groups: list[dict[str, Any]] | None = None) -> bool:
    """Добор лотов (п. 4): лоты платформы — в подробности OWS, где лотов нет. Остальное в них
    (документы, тип торгов, даты) — от OWS, не трогаем. Подробностей OWS нет — как save.
    False — лотов нет ни там, ни там или лоты уже есть."""
    platform = to_detail(card, lots, groups=groups)
    if not any(not lot["isDeleted"] for lot in platform["Lots"]):
        return False
    row = conn.execute("SELECT detail, lots_ready FROM announcements WHERE id = %s FOR UPDATE",
                       (platform["id"],)).fetchone()
    if row is None or not row[0]:
        return save(conn, card, lots, groups)
    detail, ready = row
    if ready:
        return False  # лоты уже есть (OWS успел) — его подробности полнее
    # source — лоты платформы не затирать пустыми из OWS, пока его список лотов стоит
    # (details.save); platformFiles — техспецификации лотов взяты (документы не отдались —
    # не ставим: доберёт _files, документы объявления от OWS он не тронет).
    detail.update(Lots=platform["Lots"], source="platform")
    if groups is not None:
        detail["platformFiles"] = True
    if not detail.get("finYear"):
        detail["finYear"] = platform["finYear"]  # у OWS без лотов годов нет — из пунктов плана
    _upsert_lots(conn, platform)  # номер объявления и лоты — из карточки платформы
    details.save(conn, detail)
    portal.remember(conn, [card])
    # Как в save: OWS перечитаем на ближайшем круге, а его пустые пункты плана не затрут наши.
    conn.execute("UPDATE announcements SET details_fetched_at = NULL, details_update_date = NULL WHERE id = %s",
                 (platform["id"],))
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


def fill_files(conn: psycopg.Connection, announcement_id: int, groups: list[dict[str, Any]]) -> int:
    """Дописывает документы платформы в подробности, взятые с платформы (OWS их ещё не прислал).
    Возвращает, скольким лотам нашлась техспецификация."""
    row = conn.execute("SELECT detail, details_fetched_at, details_update_date FROM announcements "
                       "WHERE id = %s FOR UPDATE", (announcement_id,)).fetchone()
    if not row or not row[0] or row[0].get("source") != "platform" or row[0].get("platformFiles"):
        return 0  # пока ждали — пришли подробности OWS (там свои файлы) или уже дописали
    detail, fetched_at, update_date = row
    own = detail.get("Files") if detail.get("platformId") is None else None  # документы OWS (добор лотов)
    add_files(detail, groups)
    if own:
        detail["Files"] = own
    if not detail.get("platformFiles"):
        return 0  # документов нет — нечего дописывать
    details.save(conn, detail)
    # Это не чтение OWS: его очередь (файлы, протоколы, статусы) не сдвигаем, а пустая дата
    # обновления (save) не даёт OWS затереть пункты плана платформы пустыми — её тоже возвращаем.
    conn.execute("UPDATE announcements SET details_fetched_at = %s, details_update_date = %s WHERE id = %s",
                 (fetched_at, update_date, announcement_id))
    return sum(1 for lot in detail.get("Lots") or [] if spec.spec_file(lot))


class Reserve:
    """Состояние между проходами: недавние неудачные попытки (чтобы не долбить одно и то же)."""

    def __init__(self, client: Client):
        self.client = client
        self.tried: dict[int, datetime] = {}
        self.files_tried: dict[int, datetime] = {}  # _files: неудачные попытки, отдельно от tried

    def _skip(self, key: int, now: datetime) -> bool:
        return (when := self.tried.get(key)) is not None and when > now - RETRY

    def _lots(self, platform_id: int) -> list[dict[str, Any]]:
        return results(self.client.get(f"/announcements/{platform_id}/lots/"))

    def _groups(self, platform_id: int) -> list[dict[str, Any]] | None:
        """Документы объявления; не отдались — None (сохраним без них, доберёт _files)."""
        try:
            return results(self.client.get(f"/announcements/{platform_id}/general-files/"))
        except PlatformError as error:
            logger.warning("reserve files %s: %s", platform_id, error)
            return None

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
                items = [i for i in results(page) if _ext(i)]
                self._remember(conn, items)  # номер платформы — для ссылки на объявление
                ready = {r[0] for r in conn.execute(
                    "SELECT id FROM announcements WHERE id = ANY(%s) AND lots_ready", ([_ext(i) for i in items],))}
                conn.commit()
                new = [i for i in items if _ext(i) not in ready and not self._skip(_ext(i), now)
                       and (parse_date(i.get("publish_date")) or now) > now - FRESH]
                todo += new
                quiet = 0 if new else quiet + 1
                offset += PAGE
                oldest = parse_date((items or [{}])[-1].get("publish_date"))
                if not isinstance(page, dict) or not page.get("next") or (oldest and oldest < now - FRESH):
                    break
        todo.sort(key=lambda i: i.get("publish_date") or "", reverse=True)
        return todo

    def run(self, conn: psycopg.Connection, now: datetime | None = None,
            limit: int = PER_RUN, plans_limit: int = PLANS_PER_RUN,
            links_limit: int = LINKS_PER_RUN, files_limit: int = FILES_PER_RUN,
            backlog_limit: int = BACKLOG_PER_RUN) -> dict[str, int]:
        now = now or datetime.now(UTC)
        stats = {"listed": 0, "saved": 0, "backlog": 0, "plans": 0, "links": 0, "specs": 0, "failed": 0}
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
            groups = self._groups(item["id"])
            if not groups:
                self.files_tried[aid] = now  # добор (_files) — не раньше чем через RETRY
            result = self._save(conn, aid, partial(save, conn, card, lots, groups))
            if result:
                stats["saved"] += 1
            else:
                self.tried[aid] = now
                stats["failed"] += result is None  # None — данные не сохранились (ошибка)
        # Свежие важнее; время, которое они не заняли (ночью почти всё), — добору.
        stats["backlog"] = self._backlog(conn, now, backlog_limit, stats,
                                         max(deadline, time.monotonic()) + BACKLOG_SECONDS)
        stats["plans"] = self._plans(conn, now, plans_limit, stats)
        # Ссылкам и документам — своё время, даже если свежие и добор лотов заняли всё своё.
        extra = max(deadline, time.monotonic()) + LINKS_SECONDS
        stats["links"] = self._links(conn, now, links_limit, stats, extra)
        # Добор документов — в то же время, что и ссылки: проход не должен затягиваться.
        stats["specs"] = self._files(conn, now, files_limit, stats, extra)
        # Память попыток — только за RETRY.
        self.tried = {k: v for k, v in self.tried.items() if v > now - RETRY}
        return stats

    def _backlog(self, conn: psycopg.Connection, now: datetime, limit: int, stats: dict[str, int],
                 deadline: float) -> int:
        """Открытые объявления OWS без лотов (п. 4 описания модуля): новые первыми. Номера
        платформы нет — ищем по номеру объявления. Не нашли или лотов нет — не раньше RETRY."""
        tried = [k for k, v in self.tried.items() if k > 0 and v > now - RETRY]
        # Не нашли на платформе или лотов нет и там — отметка в базе (как у _links), снова
        # не раньше BACKLOG_RECHECK (и после перезапуска службы).
        rows = conn.execute(
            """SELECT id, number_anno, platform_id FROM announcements
               WHERE NOT lots_ready AND system = 1 AND end_date > %s
                 AND publish_date BETWEEN %s AND %s AND NOT id = ANY(%s)
                 AND (platform_id IS NOT NULL OR number_anno IS NOT NULL)
                 AND (platform_checked_at IS NULL OR platform_checked_at < %s)
               ORDER BY publish_date DESC LIMIT %s""",
            (now, now - BACKLOG_AGE, now - BACKLOG_AFTER, tried, now - BACKLOG_RECHECK, limit)).fetchall()
        conn.commit()
        saved = 0
        for aid, number, pid in rows:
            if time.monotonic() > deadline:
                break
            self.tried[aid] = now
            try:
                if pid is None:
                    found = portal.find(self.client, aid, number)
                    if found is None:
                        self._save(conn, aid, partial(portal.missed, conn, aid))
                        continue
                    self._remember(conn, [found])
                    pid = int(found["id"])
                card = self.client.get(f"/announcements/{pid}/")
                lots = self._lots(pid)
            except (PlatformError, KeyError, TypeError, ValueError) as error:
                logger.warning("reserve backlog %s: %r", aid, error)
                stats["failed"] += 1
                continue
            # Документы (техспецификации лотов) здесь не берём — их запрос самый медленный;
            # без пометки platformFiles их доберёт _files.
            result = self._save(conn, aid, partial(fill_lots, conn, card, lots, None))
            if result is False:
                self._save(conn, aid, partial(portal.missed, conn, aid))
            saved += bool(result)
            stats["failed"] += result is None
        return saved

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
                found = portal.find(self.client, aid, number)
                if found is None:
                    continue
                self._remember(conn, [found])
                lots = self._lots(found["id"])
            except PlatformError as error:
                logger.warning("reserve plans %s: %s", aid, error)
                stats["failed"] += 1
                continue
            result = self._save(conn, aid, partial(fill_plans, conn, aid, lots))
            filled += result or 0
            stats["failed"] += result is None
        return filled


    def _remember(self, conn: psycopg.Connection, items: list[dict[str, Any]]) -> int:
        """Номера платформы — польза сверху: сбой записи не должен стоить прохода."""
        try:
            count = portal.remember(conn, items)
            conn.commit()
            return count
        except psycopg.Error as error:
            conn.rollback()
            logger.warning("reserve platform ids: %r", error)
            return 0

    def _links(self, conn: psycopg.Connection, now: datetime, limit: int, stats: dict[str, int],
               deadline: float) -> int:
        """Открытые объявления без номера платформы (старше списка резерва) — ищем по номеру:
        по нему ссылка «На портале». Сначала не искавшиеся, внутри — что закрывается раньше;
        не нашли — отметка в базе, снова не раньше чем через сутки."""
        rows = conn.execute(
            """SELECT id, number_anno FROM announcements
               WHERE platform_id IS NULL AND system = 1 AND end_date > %s AND number_anno IS NOT NULL
                 AND (platform_checked_at IS NULL OR platform_checked_at < %s)
               ORDER BY platform_checked_at NULLS FIRST, end_date LIMIT %s""",
            (now, now - portal.RECHECK, limit)).fetchall()
        conn.commit()
        linked = 0
        for aid, number in rows:
            if time.monotonic() > deadline:
                break
            try:
                found = portal.find(self.client, aid, number)
            except PlatformError as error:
                logger.warning("reserve link %s: %s", aid, error)
                stats["failed"] += 1
                continue
            if found is None:
                try:
                    portal.missed(conn, aid)
                    conn.commit()
                except psycopg.Error as error:
                    conn.rollback()
                    logger.warning("reserve link %s: %r", aid, error)
                continue
            linked += self._remember(conn, [found])
        return linked

    def _files(self, conn: psycopg.Connection, now: datetime, limit: int, stats: dict[str, int],
               deadline: float) -> int:
        """Открытые объявления с платформы, сохранённые без документов (до этой доработки или
        при сбое general-files): дописываем документы, техспецификации лотов — в их Files."""
        tried = [k for k, v in self.files_tried.items() if v > now - RETRY]
        rows = conn.execute(
            """SELECT id, platform_id FROM announcements
               WHERE detail->>'source' = 'platform' AND NOT detail ? 'platformFiles'
                 AND platform_id IS NOT NULL AND end_date > %s AND NOT id = ANY(%s)
               ORDER BY end_date LIMIT %s""",
            (now, tried, limit)).fetchall()
        conn.commit()
        found = 0
        for aid, platform_id in rows:
            if time.monotonic() > deadline:
                break
            self.files_tried[aid] = now
            if (groups := self._groups(platform_id)) is None:
                stats["failed"] += 1
                continue
            result = self._save(conn, aid, partial(fill_files, conn, aid, groups))
            found += result or 0
            stats["failed"] += result is None
        self.files_tried = {k: v for k, v in self.files_tried.items() if v > now - RETRY}
        return found


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
