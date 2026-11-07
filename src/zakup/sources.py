"""Источники для подбора товаров: справочник магазинов и поставщиков.

ИИ после каждого поиска записывает сайты, где нашёл товары (категория
товара, город, сколько раз находил); пользователи добавляют свои. Перед
поиском ИИ получает этот список и сначала смотрит подходящие источники.
"""

from typing import Any
from urllib.parse import urlsplit

import psycopg

# Сколько источников передавать ИИ: свои — все, найденные ИИ — самые полезные.
PROMPT_LIMIT = 80


def domain_of(value: str) -> str | None:
    """«https://www.Zeta.kz/catalog» или «Zeta.kz» → «zeta.kz»."""
    value = (value or "").strip()
    if not value:
        return None
    host = urlsplit(value if "://" in value else f"https://{value}").hostname or ""
    host = host.lower().removeprefix("www.")
    return host if "." in host and len(host) <= 200 else None


def categories_of(value: str) -> list[str]:
    return sorted({c.strip().capitalize() for c in (value or "").split(",") if c.strip()})[:10]


def add(conn: psycopg.Connection, user_id: int, url: str, name: str = "",
        categories: str = "", city: str = "", note: str = "") -> str | None:
    """Добавляет (или включает и дополняет) источник. Возвращает домен или None."""
    domain = domain_of(url)
    if domain is None:
        return None
    conn.execute(
        """INSERT INTO sources (domain, name, categories, city, note, added_by, auto)
           VALUES (%s, %s, %s, %s, %s, %s, false)
           ON CONFLICT (domain) DO UPDATE SET
               name = coalesce(nullif(excluded.name, ''), sources.name),
               categories = coalesce((SELECT array_agg(DISTINCT c ORDER BY c)
                                      FROM unnest(sources.categories || excluded.categories) c),
                                     '{}'),
               city = coalesce(nullif(excluded.city, ''), sources.city),
               note = coalesce(nullif(excluded.note, ''), sources.note),
               added_by = coalesce(sources.added_by, excluded.added_by),
               auto = false, active = true""",
        (domain, name.strip()[:150], categories_of(categories), city.strip()[:100],
         note.strip()[:300], user_id),
    )
    return domain


def remember(conn: psycopg.Connection, result: dict[str, Any]) -> int:
    """Записывает сайты из результата поиска: новые — как найденные ИИ."""
    category = (result.get("category") or "").strip().capitalize()[:60]
    seen = 0
    for item in result.get("items") or []:
        domain = domain_of(item.get("url") or "")
        if domain is None:
            continue
        conn.execute(
            """INSERT INTO sources (domain, name, categories, city, auto, found_count, last_found)
               VALUES (%s, %s, %s, %s, true, 1, now())
               ON CONFLICT (domain) DO UPDATE SET
                   name = coalesce(sources.name, excluded.name),
                   city = coalesce(sources.city, excluded.city),
                   categories = coalesce((SELECT array_agg(DISTINCT c ORDER BY c)
                                          FROM unnest(sources.categories || excluded.categories) c),
                                         '{}'),
                   found_count = sources.found_count + 1, last_found = now()""",
            (domain, (item.get("seller") or "")[:150] or None, [category] if category else [],
             (item.get("city") or "")[:100] or None),
        )
        seen += 1
    return seen


def for_prompt(conn: psycopg.Connection) -> str:
    """Список источников для запроса к ИИ: «домен — категории, город»."""
    rows = conn.execute(
        """SELECT domain, categories, city, auto FROM sources WHERE active
           ORDER BY auto, found_count DESC, domain LIMIT %s""",
        (PROMPT_LIMIT,),
    ).fetchall()
    lines = []
    for domain, categories, city, auto in rows:
        extra = ", ".join(filter(None, [", ".join(categories), city]))
        lines.append(f"- {domain}{' — ' + extra if extra else ''}{'' if auto else ' (добавлен вручную)'}")
    return "\n".join(lines)


def listing(conn: psycopg.Connection) -> list[tuple]:
    return conn.execute(
        """SELECT s.id, s.domain, s.name, s.categories, s.city, s.note, s.auto, s.active,
                  s.found_count, s.last_found, u.email, s.added_by
           FROM sources s LEFT JOIN users u ON u.id = s.added_by
           ORDER BY s.active DESC, s.auto, s.found_count DESC, s.domain"""
    ).fetchall()


def known_categories(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT unnest(categories) c FROM sources ORDER BY c")]
