"""Ссылка на объявление на единой платформе zakup.gov.kz.

Старый портал госзакупок отключили (v3bl.goszakup.gov.kz просит вход, old.goszakup.gov.kz —
пустая страница), объявления теперь на https://zakup.gov.kz/announcement/<номер платформы>.
Номер платформы свой, не OWS: его даёт список платформы (резерв zakup.reserve — по ходу
и добором открытых), а если ещё не знаем — переход /a/<id>/portal ищет по номеру объявления
(«17950444-1») и запоминает (announcements.platform_id, миграция 0040). Не нашли — помним
(platform_checked_at) и сутки не ищем снова.
"""

import logging
from datetime import timedelta
from typing import Any

import httpx
import psycopg

from zakup.skk import BASE_URL, PORTAL_URL, PlatformError, results

logger = logging.getLogger(__name__)
HOME = "https://zakup.gov.kz/"
SYSTEMS = (4, 1)  # госзакупки на платформе: новая система (с 23.09.2026) чаще — первой
RECHECK = timedelta(days=1)


def url(platform_id: int | None) -> str | None:
    return PORTAL_URL.format(platform_id) if platform_id else None


def ext(item: dict[str, Any]) -> int | None:
    """Номер OWS у строки платформы (external_id)."""
    value = str(item.get("external_id") or "")
    return int(value) if value.isdigit() else None


def remember(conn: psycopg.Connection, items: list[dict[str, Any]]) -> int:
    """Строки списка или карточки платформы → номера платформы наших объявлений (госзакупки).
    Занятые строки (их как раз пишет сборщик) пропускаем — не ждём и не ловим взаимную
    блокировку; номер допишется следующим проходом. Транзакцию коммитит вызывающий."""
    pairs = sorted({(e, int(i["id"])) for i in items if (e := ext(i)) and str(i.get("id") or "").isdigit()})
    if not pairs:
        return 0
    return conn.execute(
        """UPDATE announcements a SET platform_id = v.pid
           FROM unnest(%s::bigint[], %s::bigint[]) AS v(aid, pid)
           WHERE a.id = v.aid AND a.id IN (
               SELECT id FROM announcements WHERE id = ANY(%s::bigint[]) AND system = 1
               ORDER BY id FOR UPDATE SKIP LOCKED)
             AND a.platform_id IS DISTINCT FROM v.pid""",
        ([a for a, _ in pairs], [p for _, p in pairs], [a for a, _ in pairs])).rowcount


def missed(conn: psycopg.Connection, announcement_id: int) -> None:
    conn.execute("UPDATE announcements SET platform_checked_at = now() WHERE id = %s", (announcement_id,))


def find(client: Any, announcement_id: int, number: str | None) -> dict[str, Any] | None:
    """Строка платформы для нашего объявления по его номеру; None — не нашли. Ошибки
    платформы — наверх (PlatformError). client — с методом get(path, params)."""
    if not number:
        return None
    for system in SYSTEMS:
        page = client.get("/announcements/", {"system_id": system, "announcement_number": number})
        found = [i for i in results(page) if ext(i) == announcement_id]
        if found:
            return found[0]
    return None


class QuickClient:
    """Для перехода с сайта: один короткий запрос, без повторов и пауз (человек ждёт)."""

    def __init__(self, timeout: float = 5):
        self._http = httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(timeout, connect=3),
                                  headers={"Accept": "application/json"})

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._http.get(path, params=params)
        except httpx.HTTPError as error:
            raise PlatformError(f"{path}: {type(error).__name__}") from error
        if response.status_code != 200:
            raise PlatformError(f"{path}: HTTP {response.status_code}")
        return response.json()


_quick: QuickClient | None = None


def quick() -> QuickClient:
    global _quick
    if _quick is None:
        _quick = QuickClient()  # httpx.Client потокобезопасен: одно соединение на все переходы
    return _quick


def lookup(pool, client: Any, announcement_id: int) -> int | None:
    """Номер платформы: из базы или поиском на платформе (найденный — запоминаем, не найденный
    — сутки не ищем). Соединение с базой на время запроса к платформе не держим."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT platform_id, number_anno, system, platform_checked_at > now() - %s FROM announcements "
            "WHERE id = %s", (RECHECK, announcement_id)).fetchone()
    if row is None:
        return None
    platform_id, number, system, recent = row
    if platform_id or system != 1 or recent or not number:
        return platform_id
    try:
        found = find(client, announcement_id, number)
    except PlatformError as error:
        logger.warning("portal lookup %s: %s", announcement_id, error)
        return None
    with pool.connection() as conn:
        if found is None:
            missed(conn, announcement_id)
            return None
        remember(conn, [found])
    return int(found["id"])
