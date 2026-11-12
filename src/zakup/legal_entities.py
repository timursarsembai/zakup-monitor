"""Реестр юридических лиц из открытых данных data.egov.kz (набор gbd_ul, ~1 млн записей).

Ключ не нужен: «подробный» адрес набора принимает запрос Elasticsearch в параметре source.
Берём по списку БИН (до BATCH за запрос): названия заказчиков Самрук-Казыны, которых нет
ни на платформе zakup.gov.kz, ни в OWS. Таблица legal_entities — основа будущего справочника
организаций. У одного БИН бывает несколько записей (история адресов) — берём самую свежую.
"""

import json
import logging
import time
from typing import Any

import httpx
import psycopg

logger = logging.getLogger(__name__)

URL = "https://data.egov.kz/api/detailed/gbd_ul/v1"
BATCH = 100
# Не нашли или нашли — перепроверяем не чаще раза в столько дней.
REFRESH_DAYS = 30


def _date(record: dict[str, Any]) -> str:
    return str(record.get("datereg") or "")[:10]


def fetch(http: httpx.Client, bins: list[str]) -> dict[str, dict[str, Any]]:
    """Записи реестра по БИН: {бин: запись}. Сетевые ошибки — наверх."""
    query = {"size": 1000, "query": {"terms": {"bin": bins}}}
    response = http.get(URL, params={"source": json.dumps(query)})
    response.raise_for_status()
    found: dict[str, dict[str, Any]] = {}
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"unexpected registry response: {type(body).__name__}")
    for record in body.get("data") or []:
        bin_ = record.get("bin")
        if bin_ and (bin_ not in found or _date(record) > _date(found[bin_])):
            found[bin_] = record
    return found


def ensure(conn: psycopg.Connection, bins: list[str], http: httpx.Client | None = None,
           delay: float = 1.0) -> int:
    """Догружает в legal_entities БИН, которых нет или они устарели. Возвращает, сколько
    запросили. Ошибка сети — не беда: названия подставятся на следующем проходе."""
    wanted = sorted({b for b in bins if b and b.isdigit() and len(b) == 12})
    if not wanted:
        return 0
    fresh = {row[0] for row in conn.execute(
        "SELECT bin FROM legal_entities WHERE bin = ANY(%s) AND fetched_at > now() - make_interval(days => %s)",
        (wanted, REFRESH_DAYS))}
    missing = [b for b in wanted if b not in fresh]
    conn.commit()  # сеть ждём без открытой транзакции
    if not missing:
        return 0
    own = http is None
    http = http or httpx.Client(timeout=httpx.Timeout(60, connect=5))
    try:
        for start in range(0, len(missing), BATCH):
            chunk = missing[start:start + BATCH]
            try:
                found = fetch(http, chunk)
            except (httpx.HTTPError, ValueError) as error:
                logger.warning("legal entities: %s", error)
                return start
            for bin_ in chunk:
                r = found.get(bin_) or {}
                conn.execute(
                    """INSERT INTO legal_entities (bin, name_ru, name_kz, address_ru, director, oked_ru,
                                                   status_ru, registered, fetched_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                       ON CONFLICT (bin) DO UPDATE SET
                           name_ru = coalesce(excluded.name_ru, legal_entities.name_ru),
                           name_kz = coalesce(excluded.name_kz, legal_entities.name_kz),
                           address_ru = coalesce(excluded.address_ru, legal_entities.address_ru),
                           director = coalesce(excluded.director, legal_entities.director),
                           oked_ru = coalesce(excluded.oked_ru, legal_entities.oked_ru),
                           status_ru = coalesce(excluded.status_ru, legal_entities.status_ru),
                           registered = coalesce(excluded.registered, legal_entities.registered),
                           fetched_at = now()""",
                    (bin_, r.get("nameru"), r.get("namekz"), r.get("addressru"), r.get("director"),
                     r.get("okedru"), r.get("statusru"), _date(r) or None))
            conn.commit()
            if start + BATCH < len(missing):
                time.sleep(delay)
    finally:
        if own:
            http.close()
    return len(missing)


def names(conn: psycopg.Connection, bins: list[str]) -> dict[str, str]:
    return dict(conn.execute(
        "SELECT bin, name_ru FROM legal_entities WHERE bin = ANY(%s) AND name_ru IS NOT NULL",
        (sorted({b for b in bins if b}),)).fetchall())
