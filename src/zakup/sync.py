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
    last_id: int
    caught_up: bool


def sync(conn: psycopg.Connection, client: Client, entity: str, max_pages: int) -> SyncResult:
    """Загружает записи новее last_id, идя от самой новой вниз.

    Состояние сохраняется после каждой страницы, так что прерванный проход
    продолжится с того же места, а не начнётся заново сверху.
    """
    state = db.get_state(conn, entity)
    if state is None:
        raise RuntimeError(f"{entity}: нет начальной точки, сначала `zakup start-from`")
    upsert = db.UPSERT[entity]
    last_id, walk_top, after = state
    saved = 0
    for _ in range(max_pages):
        page = client.page(entity, after=after)
        items = page.items
        if walk_top is None:
            if not items or int(items[0]["id"]) <= last_id:
                return SyncResult(entity, saved, last_id, True)
            walk_top = int(items[0]["id"])
        fresh = [item for item in items if int(item["id"]) > last_id]
        for item in fresh:
            upsert(conn, item)
        saved += len(fresh)
        done = len(fresh) < len(items) or page.next_after is None
        if done:
            # Дошли до уже загруженного: всё до верха прохода есть, следующий — снова сверху.
            last_id, walk_top, after = walk_top, None, None
        else:
            after = page.next_after
        db.save_state(conn, entity, db.SyncState(last_id, walk_top, after))
        conn.commit()
        if done:
            return SyncResult(entity, saved, last_id, True)
    return SyncResult(entity, saved, last_id, False)


def find_start(
    client: Client, entity: str, reached: Callable[[dict[str, Any]], bool]
) -> int | None:
    """Находит id самой старой записи, для которой `reached` истинна.

    `reached` должна быть монотонной по id: ложь для старых записей, истина для
    новых. Бинарный поиск по id, а не листание страниц: записей миллионы.
    Возвращает None, если подходящих записей нет.
    """

    def at_most(x: int) -> dict[str, Any] | None:
        # search_after=x+1 отдаёт первую запись с id <= x.
        items = client.page(entity, after=x + 1, limit=1).items
        return items[0] if items else None

    newest = client.page(entity, limit=1).items
    if not newest or not reached(newest[0]):
        return None
    # Инвариант: запись с id <= hi подходит, с id <= lo — нет или её нет вовсе.
    lo, hi = 0, int(newest[0]["id"])
    while hi - lo > 1:
        mid = (lo + hi) // 2
        item = at_most(mid)
        if item is not None and reached(item):
            hi = int(item["id"])
        else:
            lo = mid
    return hi


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


def rescan(
    conn: psycopg.Connection, client: Client, entity: str, floor_id: int, max_pages: int
) -> int:
    """Перечитывает записи с id >= floor_id сверху вниз, по max_pages страниц за вызов.

    Нужен потому, что id выдаётся черновику: объявление, опубликованное через
    несколько дней, появляется в API ниже уже загруженного, и `sync` его не увидит.
    Заодно обновляет статусы. Место остановки хранится в sync_state под
    ключом rescan:<entity>, проход идёт по кругу.
    """
    key = f"rescan:{entity}"
    state = db.get_state(conn, key)
    after = state.resume_after if state else None
    upsert = db.UPSERT[entity]
    saved = 0
    for _ in range(max_pages):
        page = client.page(entity, after=after)
        items = [item for item in page.items if int(item["id"]) >= floor_id]
        for item in items:
            upsert(conn, item)
        saved += len(items)
        done = len(items) < len(page.items) or page.next_after is None
        after = None if done else page.next_after
        db.save_state(conn, key, db.SyncState(floor_id, None, after))
        conn.commit()
        if done:
            break
    return saved


def backfill(
    conn: psycopg.Connection, client: Client, entity: str, max_pages: int,
    on_page: Callable[[list[int]], None] | None = None,
    keep_going: Callable[[], bool] | None = None,
) -> int | None:
    """Догружает историю вниз, до id из sync_state backfill:<entity> (`zakup backfill-from`).

    Состояние: last_id — нижняя граница, resume_after — курсор (None — догружено).
    on_page получает id сохранённых записей до коммита страницы. Возвращает число
    сохранённых записей или None, если догружать нечего. keep_going — проверка перед
    каждой страницей (кнопка «Остановить»).
    """
    key = f"backfill:{entity}"
    state = db.get_state(conn, key)
    if state is None or state.resume_after is None:
        return None
    floor_id, after = state.last_id, state.resume_after
    upsert = db.UPSERT[entity]
    saved = 0
    for _ in range(max_pages):
        if keep_going and not keep_going():
            break
        page = client.page(entity, after=after)
        items = [item for item in page.items if int(item["id"]) >= floor_id]
        for item in items:
            upsert(conn, item)
        if on_page and items:
            on_page([int(item["id"]) for item in items])
        saved += len(items)
        done = len(items) < len(page.items) or page.next_after is None
        after = None if done else page.next_after
        db.save_state(conn, key, db.SyncState(floor_id, None, after))
        conn.commit()
        if done:
            break
    return saved
