"""Статистика посещений: кто заходит (включая гостей), откуда, с чего, во сколько.

Пишется после ответа, в фоне, и только для открытия страниц (не фрагменты и не
файлы). Посетителя отличает случайная метка в cookie zakup_vid; IP-адрес не
храним — только страну, область и город (geo.py). Подробные записи живут
RETENTION_DAYS, потом удаляются.
"""

import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg

from zakup import geo

logger = logging.getLogger(__name__)

COOKIE = "zakup_vid"
COOKIE_AGE = 2 * 365 * 86400
RETENTION_DAYS = 90
EVENTS_RETENTION_DAYS = 180

_BOT = re.compile(r"bot|crawl|spider|slurp|facebookexternalhit|preview|curl|wget|python|httpx|"
                  r"go-http|java/|headless|monitor|uptime|scan|okhttp|axios|node-fetch|libwww", re.I)
_SEARCH = {"google": "google", "yandex": "yandex", "bing": "bing", "mail.ru": "mail.ru", "duckduckgo": "duckduckgo"}
_SOCIAL = {"t.me": "telegram", "telegram": "telegram", "web.telegram": "telegram", "whatsapp": "whatsapp",
           "wa.me": "whatsapp", "instagram": "instagram", "facebook": "facebook", "vk.com": "vk",
           "youtube": "youtube", "linkedin": "linkedin", "threads": "threads"}
# Ответы-фрагменты, которые страница подгружает сама: это не посещения.
_FRAGMENTS = re.compile(r"^/(static/|lots/\d+/(research|questions|spec)|a/\d+/|pay/[^/]+/status|"
                        r"settings/(overview|ai/models)|analytics/trends|dashboard/data|t/|favicon|robots|sitemap)")
# Параметры поиска, которые храним (без личного: телефоны, коды не попадают в адреса страниц).
_KEEP_QUERY = 600


def new_visitor_id() -> str:
    # Время создания в начале — «новый или вернувшийся» видно и после удаления старых записей.
    return f"{int(time.time()):x}.{secrets.token_urlsafe(9)}"


def first_seen(visitor: str | None) -> int | None:
    try:
        return int((visitor or "").split(".", 1)[0], 16)
    except ValueError:
        return None


def is_bot(user_agent: str | None) -> bool:
    return not user_agent or bool(_BOT.search(user_agent))


def device(user_agent: str) -> str:
    ua = user_agent.lower()
    if "ipad" in ua or "tablet" in ua or ("android" in ua and "mobile" not in ua):
        return "tablet"
    if "mobi" in ua or "iphone" in ua or "android" in ua:
        return "mobile"
    return "desktop"


def browser(user_agent: str) -> str:
    ua = user_agent
    for name, pattern in (("Яндекс Браузер", "YaBrowser"), ("Edge", "Edg/"), ("Opera", "OPR/"),
                          ("Samsung", "SamsungBrowser"), ("Telegram", "Telegram"), ("Chrome", "CriOS"),
                          ("Firefox", "FxiOS"), ("Chrome", "Chrome/"), ("Firefox", "Firefox/"),
                          ("Safari", "Safari/")):
        if pattern in ua:
            return name
    return "другой"


def source(referrer: str | None, own_host: str) -> str:
    host = (urlsplit(referrer or "").hostname or "").lower().removeprefix("www.")
    if not host:
        return "direct"
    if host == own_host or host.endswith("." + own_host):
        return "internal"
    for key, name in {**_SEARCH, **_SOCIAL}.items():
        if key in host:
            return name
    return f"other:{host[:60]}"


def utm(query: str) -> str | None:
    params = parse_qs(query)
    parts = [params.get(k, [""])[0][:60] for k in ("utm_source", "utm_campaign")]
    return " / ".join(p for p in parts if p) or None


def wanted(method: str, path: str, status: int, content_type: str, fetch_dest: str | None) -> bool:
    """Открытие страницы (а не фрагмент, файл или запрос скрипта)."""
    if method != "GET" or _FRAGMENTS.match(path):
        return False
    if fetch_dest is not None and fetch_dest != "document":
        return False
    return "text/html" in content_type or status >= 500


def record(conn: psycopg.Connection, *, visitor: str | None, user_id: int | None, path: str,
           query: str, referrer: str | None, user_agent: str | None, ip: str | None,
           own_host: str, status: int, ms: int) -> None:
    ua = user_agent or ""
    bot = is_bot(ua)
    country, region, city = (None, None, None) if bot else geo.lookup(conn, ip)
    conn.execute(
        """INSERT INTO visits (visitor, user_id, path, query, source, utm, device, browser,
                               country, region, city, is_bot, status, ms)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (visitor, user_id, path[:300], query[:_KEEP_QUERY] or None, source(referrer, own_host),
         utm(query), device(ua) if not bot else None, browser(ua) if not bot else None,
         country, region, city, bot, status, ms),
    )
    conn.commit()


EVENT_NAMES = {"paywall", "search", "view_switch", "theme", "lang"}


def event(conn: psycopg.Connection, name: str, visitor: str | None, user_id: int | None,
          props: dict[str, Any] | None = None) -> None:
    if name not in EVENT_NAMES:
        return
    conn.execute("INSERT INTO events (visitor, user_id, name, props) VALUES (%s, %s, %s, %s)",
                 (visitor, user_id, name, json.dumps(props or {}, ensure_ascii=False)[:2000]))
    conn.commit()


def purge(conn: psycopg.Connection) -> tuple[int, int]:
    gone = conn.execute(f"DELETE FROM visits WHERE at < now() - interval '{RETENTION_DAYS} days'").rowcount
    old = conn.execute(f"DELETE FROM events WHERE at < now() - interval '{EVENTS_RETENTION_DAYS} days'").rowcount
    conn.commit()
    return gone, old
