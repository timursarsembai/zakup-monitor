"""Карточки организаций (заказчики, организаторы) с портала: телефоны, e-mail, сайт, адреса.

В REST и GraphQL портала организацию отдают только по одному БИН за запрос, поэтому
загружаем их фоном: сначала заказчиков открытых объявлений, потом по мере появления.
Таблица subjects пригодится и для справочника поставщиков.
"""

import logging
import re
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zakup.contacts import normalize
from zakup.goszakup import Client, GoszakupError

logger = logging.getLogger(__name__)

REFRESH_AFTER = "30 days"
FIELDS = """bin iin nameRu nameKz fullNameRu fullNameKz regdate phone email website katoList
  refKopfCode customer supplier organizer okedList Address { addressType address phone katoCode }"""
QUERY = "{ Subjects(filter: {bin: \"%s\"}) { " + FIELDS + " } }"
# ИП портал ищет по ИИН, и по одному ИИН бывает две записи: человек и его ИП.
QUERY_IIN = "{ Subjects(filter: {iin: \"%s\"}) { " + FIELDS + " } }"
_BIN = re.compile(r"^\d{12}$")


def is_iin(number: str | None) -> bool:
    """ИИН физлица (и ИП): 5-я цифра — первая цифра дня рождения (0–3); у БИН там тип (4–6)."""
    return bool(number and _BIN.match(number) and number[4] in "0123")


def fetch(client: Client, bin_: str) -> dict[str, Any] | None:
    if not _BIN.match(bin_ or ""):
        return None
    if not is_iin(bin_):
        return (client.graphql(QUERY % bin_).get("Subjects") or [None])[0]
    found = client.graphql(QUERY_IIN % bin_).get("Subjects") or []
    # Из записей «человек» и «ИП» — ИП: у него дата регистрации и рабочие контакты.
    found.sort(key=lambda item: (not (item.get("nameRu") or "").upper().startswith("ИП"),
                                 not item.get("regdate"), not item.get("email")))
    return found[0] if found else None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10]) if value else None
    except ValueError:
        return None


def save(conn: psycopg.Connection, bin_: str, subject: dict[str, Any] | None) -> None:
    subject = subject or {}
    addresses, seen = [], set()
    # Сначала юридический адрес (тип 1), потом фактический и прочие.
    for item in sorted(subject.get("Address") or [], key=lambda a: a.get("addressType") or 9):
        text = " ".join((item.get("address") or "").split()).strip(" ,")
        if text and text.lower() not in seen:
            seen.add(text.lower())
            addresses.append({"address": text, "phone": item.get("phone") or "",
                              "kato": item.get("katoCode") or ""})
    kato = subject.get("katoList") or [a["kato"] for a in addresses if a["kato"]]
    conn.execute(
        """INSERT INTO subjects (bin, name_ru, name_kz, phone, email, website, addresses, fetched_at,
                                 full_name_ru, full_name_kz, reg_date, kopf, kato, is_customer, is_supplier, oked)
           VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (bin) DO UPDATE SET name_ru = excluded.name_ru, name_kz = excluded.name_kz,
               phone = excluded.phone, email = excluded.email, website = excluded.website,
               addresses = excluded.addresses, fetched_at = now(), full_name_ru = excluded.full_name_ru,
               full_name_kz = excluded.full_name_kz, reg_date = excluded.reg_date, kopf = excluded.kopf,
               kato = excluded.kato, is_customer = excluded.is_customer,
               is_supplier = excluded.is_supplier, oked = excluded.oked""",
        (bin_, subject.get("nameRu"), subject.get("nameKz"), subject.get("phone") or "",
         subject.get("email") or "", subject.get("website") or "", Jsonb(addresses),
         subject.get("fullNameRu"), subject.get("fullNameKz"), _date(subject.get("regdate")),
         # У ИП портал пишет форму «ИНД».
         {"ИНД": "ИП"}.get(subject.get("refKopfCode"), subject.get("refKopfCode")) or None, kato or None,
         bool(subject.get("customer") or subject.get("organizer")) if subject else None,
         bool(subject.get("supplier")) if subject else None,
         subject.get("okedList") if isinstance(subject.get("okedList"), int) else None),
    )
    conn.commit()


def refresh(conn: psycopg.Connection, client: Client, limit: int = 300) -> int:
    """Догружает карточки, которых нет или они устарели: сначала заказчиков и организаторов
    открытых объявлений, потом победителей (для страниц организаций), потом заказчиков
    прошлых закупок, потом всех участников. Обычные запросы к API портала, по одному БИН."""
    bins = [row[0] for row in conn.execute(
        f"""SELECT b FROM (
                SELECT DISTINCT ON (b) b, priority FROM (
                    SELECT unnest(ARRAY[customer_bin, org_bin]) AS b,
                           CASE WHEN end_date > now() THEN 0 ELSE 2 END AS priority
                    FROM announcements WHERE system = 1  -- организаций Самрук-Казыны в OWS нет
                    UNION ALL
                    SELECT winner_bin, 1 FROM lot_results WHERE outcome = 'winner'
                    UNION ALL
                    SELECT o->>'bin', 3 FROM lot_results r, jsonb_array_elements(r.offers) o
                ) wanted ORDER BY b, priority) ranked
            LEFT JOIN subjects s ON s.bin = ranked.b
            WHERE b ~ '^\\d{{12}}$'
              AND (s.bin IS NULL OR s.fetched_at < now() - interval '{REFRESH_AFTER}')
            ORDER BY priority
            LIMIT %s""",
        (limit,),
    )]
    # Ответа портала ждём без открытой транзакции: иначе она держит таблицы и не даёт
    # пройти миграциям (DeadlockDetected, LockNotAvailable).
    conn.commit()
    done = 0
    for bin_ in bins:
        try:
            data = fetch(client, bin_)
            save(conn, bin_, data)
            conn.commit()
            done += 1
        except GoszakupError as error:
            logger.info("subject %s: %s", bin_, error)
    return done


def ensure(conn: psycopg.Connection, client: Client, *bins: str | None) -> None:
    """Для страниц объявления и организации: подгрузить недостающие и устаревшие карточки сразу."""
    for bin_ in dict.fromkeys(b for b in bins if b and _BIN.match(b)):
        if not conn.execute(
                f"SELECT 1 FROM subjects WHERE bin = %s AND fetched_at > now() - interval '{REFRESH_AFTER}'",
                (bin_,)).fetchone():
            conn.commit()  # ждём портал без открытой транзакции
            try:
                data = fetch(client, bin_)
                save(conn, bin_, data)
                conn.commit()
            except GoszakupError as error:
                logger.info("subject %s: %s", bin_, error)


def _contact(raw: str, label: str = "") -> dict[str, str] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    # В карточке портала номер бывает без 8 и +7: «7075060312» — это 8 707 506 03 12.
    if re.fullmatch(r"[1-9]\d{9}", re.sub(r"[\s()-]", "", raw)):
        raw = "8" + re.sub(r"[\s()-]", "", raw)
    number = normalize(raw)
    digits = re.sub(r"[^\d+]", "", number or raw)
    mobile = bool(number) and number[5] in "04567"
    return {"number": number or raw, "tel": digits, "label": label,
            "wa": "https://wa.me/" + digits.lstrip("+") if mobile else ""}


def card(conn: psycopg.Connection, bin_: str | None) -> dict[str, Any] | None:
    """Контакты организации для страниц: телефоны со ссылками, e-mail, сайт, адреса."""
    if not bin_:
        return None
    row = conn.execute(
        """SELECT name_ru, name_kz, phone, email, website, addresses, full_name_ru, full_name_kz,
                  reg_date, kopf FROM subjects WHERE bin = %s""",
        (bin_,),
    ).fetchone()
    if row is None:
        return None
    name_ru, name_kz, phone, email, website, addresses, full_ru, full_kz, reg_date, kopf = row
    phones = []
    for raw, label in [(p, "") for p in re.split(r"[,;]", phone or "")] + [
            (a["phone"], "") for a in addresses or []]:
        contact = _contact(raw, label)
        if contact and all(c["tel"] != contact["tel"] for c in phones):
            phones.append(contact)
    site = (website or "").strip()
    if site and not site.startswith(("http://", "https://")):
        site = "https://" + site
    return {"bin": bin_, "name_ru": name_ru, "name_kz": name_kz, "phones": phones,
            "email": (email or "").strip(), "website": site,
            "addresses": [a["address"] for a in addresses or []],
            "full_name_ru": full_ru, "full_name_kz": full_kz, "reg_date": reg_date, "kopf": kopf,
            "id_label": "ИИН" if is_iin(bin_) else "БИН"}


def phones_in_text(*texts: str | None) -> list[dict[str, str]]:
    """Телефоны, которые заказчик написал в описании лота."""
    from zakup.contacts import labeled_phones

    found: list[dict[str, str]] = []
    for text in texts:
        if not text:
            continue
        for item in labeled_phones(text.replace("\n", "<br>")):
            contact = _contact(item["number"], item["label"])
            if contact and all(c["tel"] != contact["tel"] for c in found):
                found.append(contact)
    return found
