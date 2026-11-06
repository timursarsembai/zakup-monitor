import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, NamedTuple

import psycopg

from zakup import db
from zakup.goszakup import Client, parse_date

logger = logging.getLogger(__name__)


class SyncResult(NamedTuple):
    entity: str
    saved: int
    last_id: int | None
    caught_up: bool


def sync(conn: psycopg.Connection, client: Client, entity: str, max_pages: int) -> SyncResult:
    """Догружает записи с id больше сохранённого курсора. Курсор пишется после каждой страницы."""
    after = db.get_cursor(conn, entity)
    upsert = db.UPSERT[entity]
    saved = 0
    for _ in range(max_pages):
        page = client.page(entity, after=after)
        if not page.items:
            return SyncResult(entity, saved, after, True)
        for item in page.items:
            upsert(conn, item)
        saved += len(page.items)
        after = page.next_after or max(int(item["id"]) for item in page.items)
        db.set_cursor(conn, entity, after)
        conn.commit()
        if page.next_after is None:
            return SyncResult(entity, saved, after, True)
    return SyncResult(entity, saved, after, False)


def find_start(
    client: Client, entity: str, reached: Callable[[dict[str, Any]], bool]
) -> int | None:
    """Ищет курсор, с которого начинается нужный период, чтобы не качать всю историю.

    `reached(item)` должна быть монотонной по id: ложь для старых записей, истина для
    новых. Возвращает search_after для первой записи, где она истинна, или None,
    если такой записи нет.
    """

    def first_after(after: int) -> dict[str, Any] | None:
        items = client.page(entity, after=after, limit=1).items
        return items[0] if items else None

    first = first_after(0)
    if first is None:
        return None
    if reached(first):
        return 0
    # Сначала нащупываем верхнюю границу удвоением, затем бинарный поиск.
    lo, hi = int(first["id"]) - 1, max(int(first["id"]) * 2, 1024)
    while (item := first_after(hi)) is not None and not reached(item):
        lo, hi = hi, hi * 2
    # Инвариант: после lo идёт старая запись, после hi — новая или ничего.
    while hi - lo > 1:
        mid = (lo + hi) // 2
        item = first_after(mid)
        if item is None or reached(item):
            hi = mid
        else:
            lo = mid
    item = first_after(hi)
    return hi if item is not None and reached(item) else None


def published_since(since: datetime) -> Callable[[dict[str, Any]], bool]:
    def reached(item: dict[str, Any]) -> bool:
        published = parse_date(item.get("publish_date"))
        return published is not None and published >= since

    return reached


def lot_of_announcement_from(first_announcement_id: int) -> Callable[[dict[str, Any]], bool]:
    def reached(item: dict[str, Any]) -> bool:
        trd_buy_id = item.get("trd_buy_id")
        return trd_buy_id is not None and int(trd_buy_id) >= first_announcement_id

    return reached
