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
CATEGORY_LIMIT = 40
# Источников на проверке от одного клиента — не больше стольких.
PENDING_PER_USER = 20


def _plain(value: str, limit: int) -> str:
    """Короткий простой текст: буквы, цифры, пробел, дефис, точка. Он попадает в
    подсказку ИИ у всех клиентов — длинный текст был бы уже указаниями, а не категорией."""
    kept = "".join(c for c in (value or "") if c.isalnum() or c in " -.")
    return " ".join(kept.split())[:limit].strip()


def domain_of(value: str) -> str | None:
    """«https://www.Zeta.kz/catalog» или «Zeta.kz» → «zeta.kz»."""
    value = (value or "").strip()
    if not value:
        return None
    host = urlsplit(value if "://" in value else f"https://{value}").hostname or ""
    host = host.lower().removeprefix("www.")
    return host if "." in host and len(host) <= 200 else None


def categories_of(value: str) -> list[str]:
    return sorted({c for c in (_plain(c, CATEGORY_LIMIT).capitalize() for c in (value or "").split(","))
                   if c})[:10]


class TooManyPending(Exception):
    pass


def add(conn: psycopg.Connection, user_id: int, url: str, name: str = "",
        categories: str = "", city: str = "", note: str = "", staff: bool = False) -> str | None:
    """Добавляет источник. Возвращает домен или None.

    Сотрудник добавляет сразу (или включает и дополняет существующий). Клиент — на
    проверку: в подбор у всех источник попадёт после одобрения сотрудником, а уже
    известный сайт клиент не меняет."""
    domain = domain_of(url)
    if domain is None:
        return None
    if not staff:
        pending = conn.execute("SELECT count(*) FROM sources WHERE added_by = %s AND NOT checked",
                               (user_id,)).fetchone()[0]
        if pending >= PENDING_PER_USER:
            raise TooManyPending()
        conn.execute(
            """INSERT INTO sources (domain, name, categories, city, note, added_by, auto, checked)
               VALUES (%s, %s, %s, %s, %s, %s, false, false) ON CONFLICT (domain) DO NOTHING""",
            (domain, name.strip()[:150], categories_of(categories), _plain(city, 60),
             note.strip()[:300], user_id),
        )
        return domain
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
               auto = false, active = true, checked = true""",
        (domain, name.strip()[:150], categories_of(categories), _plain(city, 60),
         note.strip()[:300], user_id),
    )
    return domain


def remember(conn: psycopg.Connection, result: dict[str, Any]) -> int:
    """Записывает сайты из результата поиска: новые — как найденные ИИ."""
    category = _plain(result.get("category") or "", CATEGORY_LIMIT).capitalize()
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
             _plain(item.get("city") or "", 60) or None),
        )
        seen += 1
    return seen


def for_prompt(conn: psycopg.Connection) -> str:
    """Список источников для запроса к ИИ: «домен — категории, город»."""
    rows = conn.execute(
        """SELECT domain, categories, city, auto FROM sources WHERE active AND checked
           ORDER BY auto, found_count DESC, domain LIMIT %s""",
        (PROMPT_LIMIT,),
    ).fetchall()
    lines = []
    for domain, categories, city, auto in rows:
        extra = ", ".join(filter(None, [", ".join(categories), city]))
        lines.append(f"- {domain}{' — ' + extra if extra else ''}{'' if auto else ' (добавлен вручную)'}")
    return "\n".join(lines)


def listing(conn: psycopg.Connection, viewer: int | None = None, staff: bool = True) -> list[tuple]:
    """Источники для страницы. Непроверенные видят сотрудники и тот, кто добавил."""
    return conn.execute(
        """SELECT s.id, s.domain, s.name, s.categories, s.city, s.note, s.auto, s.active,
                  s.found_count, s.last_found, u.email, s.added_by,
                  (SELECT count(*) FROM seller_contacts c
                   WHERE c.domain = s.domain AND jsonb_array_length(c.phones) > 0),
                  s.checked
           FROM sources s LEFT JOIN users u ON u.id = s.added_by
           WHERE s.checked OR %s OR s.added_by = %s
           ORDER BY s.checked, s.active DESC, s.auto, s.found_count DESC, s.domain""",
        (staff, viewer),
    ).fetchall()


def known_categories(conn: psycopg.Connection) -> list[str]:
    """Категории для подсказки ИИ — самые частые, только из проверенных источников."""
    return [r[0] for r in conn.execute(
        """SELECT c FROM sources, unnest(categories) c WHERE checked
           GROUP BY c ORDER BY count(*) DESC, c LIMIT 60""")]
