"""Карточки организаций (заказчики, организаторы) с портала: телефоны, e-mail, сайт, адреса.

В REST и GraphQL портала организацию отдают только по одному БИН за запрос, поэтому
загружаем их фоном: сначала заказчиков открытых объявлений, потом по мере появления.
Таблица subjects пригодится и для справочника поставщиков.
"""

import logging
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zakup.contacts import normalize
from zakup.goszakup import Client, GoszakupError

logger = logging.getLogger(__name__)

REFRESH_AFTER = "30 days"
QUERY = """{ Subjects(filter: {bin: "%s"}) { bin nameRu nameKz phone email website
  Address { addressType address phone } } }"""
_BIN = re.compile(r"^\d{12}$")


def fetch(client: Client, bin_: str) -> dict[str, Any] | None:
    if not _BIN.match(bin_ or ""):
        return None
    found = (client.graphql(QUERY % bin_).get("Subjects") or [None])[0]
    return found


def save(conn: psycopg.Connection, bin_: str, subject: dict[str, Any] | None) -> None:
    subject = subject or {}
    addresses, seen = [], set()
    for item in subject.get("Address") or []:
        text = " ".join((item.get("address") or "").split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            addresses.append({"address": text, "phone": item.get("phone") or ""})
    conn.execute(
        """INSERT INTO subjects (bin, name_ru, name_kz, phone, email, website, addresses, fetched_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (bin) DO UPDATE SET name_ru = excluded.name_ru, name_kz = excluded.name_kz,
               phone = excluded.phone, email = excluded.email, website = excluded.website,
               addresses = excluded.addresses, fetched_at = now()""",
        (bin_, subject.get("nameRu"), subject.get("nameKz"), subject.get("phone") or "",
         subject.get("email") or "", subject.get("website") or "", Jsonb(addresses)),
    )
    conn.commit()


def refresh(conn: psycopg.Connection, client: Client, limit: int = 300) -> int:
    """Догружает заказчиков открытых объявлений, которых нет или карточка устарела."""
    bins = [row[0] for row in conn.execute(
        f"""SELECT b FROM (
                SELECT DISTINCT unnest(ARRAY[a.customer_bin, a.org_bin]) AS b
                FROM announcements a WHERE a.end_date > now()) open_bins
            LEFT JOIN subjects s ON s.bin = open_bins.b
            WHERE b ~ '^\\d{{12}}$'
              AND (s.bin IS NULL OR s.fetched_at < now() - interval '{REFRESH_AFTER}')
            LIMIT %s""",
        (limit,),
    )]
    done = 0
    for bin_ in bins:
        try:
            save(conn, bin_, fetch(client, bin_))
            done += 1
        except GoszakupError as error:
            logger.info("subject %s: %s", bin_, error)
    return done


def ensure(conn: psycopg.Connection, client: Client, *bins: str | None) -> None:
    """Для страницы объявления: подгрузить недостающих сразу."""
    for bin_ in dict.fromkeys(b for b in bins if b and _BIN.match(b)):
        if not conn.execute("SELECT 1 FROM subjects WHERE bin = %s", (bin_,)).fetchone():
            try:
                save(conn, bin_, fetch(client, bin_))
            except GoszakupError as error:
                logger.info("subject %s: %s", bin_, error)


def _contact(raw: str, label: str = "") -> dict[str, str] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
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
        "SELECT name_ru, name_kz, phone, email, website, addresses FROM subjects WHERE bin = %s",
        (bin_,),
    ).fetchone()
    if row is None:
        return None
    name_ru, name_kz, phone, email, website, addresses = row
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
            "addresses": [a["address"] for a in addresses or []]}


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
