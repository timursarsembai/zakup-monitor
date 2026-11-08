"""Товары с Kaspi через прокси — готовым списком для ИИ (запрос «как у покупателя» составляет лёгкая модель)."""


import logging
from typing import Any

import httpx
import psycopg

from zakup import kaspi
from zakup.research import providers as prov
from zakup.research.prompts import (
    QUERY_PROMPT,
    QUERY_SCHEMA,
)
from zakup.research.providers import (
    ResearchError,
)

logger = logging.getLogger(__name__)


def _kaspi_candidates(conn: psycopg.Connection, context: dict[str, Any],
                      base: dict[str, Any] | None) -> str:
    """Товары с Kaspi через прокси — готовым списком для ИИ. Без прокси — пусто.

    Запрос «как у покупателя» составляет дешёвая модель (доли тенге, секунды).
    """
    proxy = kaspi.proxy_url(conn)
    if not proxy:
        return ""
    city = kaspi.city_of(context.get("kato"))
    name = " ".join(context["name"].split())
    extra = " ".join((context.get("extra") or "").split())[:200]
    # Канцелярское «Электроутюг» Kaspi не понимает и выдаёт что попало — запрос
    # покупателя составляет дешёвая модель; не вышло — ищем по названию лота.
    try:
        conn.commit()
        spec = (context.get("spec") or "")[:1200]
        raw = prov._light_json(conn, QUERY_PROMPT.format(name=name, extra=extra, spec=spec), QUERY_SCHEMA)
        queries = [str(q).strip()[:80] for q in raw.get("queries") or [] if str(q).strip()][:2]
    except (ResearchError, httpx.HTTPError) as error:
        logger.info("kaspi query for %r: %s", name, error)
        queries = []
    conn.commit()
    found: list[dict[str, Any]] = []
    for query in queries or [name]:
        try:
            found += [i for i in kaspi.search(query, proxy, city)
                      if i["url"] not in {f["url"] for f in found}]
        except kaspi.Blocked as error:
            logger.info("kaspi search %r: %s", query, error)
            break
    conn.commit()
    known = {i.get("url") for i in (base or {}).get("items", [])}
    return kaspi.candidates_for_prompt([i for i in found if i["url"] not in known][:kaspi.LIMIT])
