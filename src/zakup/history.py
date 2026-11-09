"""Догрузка истории в прошлое: запуск и остановка из настроек, шаг службы history.

Запуск — строка backfill_runs (дата, статус). Служба history раз в минуту делает шаг:
новый запуск — находит на портале границы (первые объявление и лот после даты), дальше
листает вниз от самых старых записей в базе. Сначала объявления, потом лоты: фильтры
подписок сверяют лот с его объявлением. Место остановки — в sync_state backfill:<сущность>,
поэтому прерванная или остановленная догрузка при новом запуске продолжается, а не
начинается заново.
"""

from datetime import date, datetime
from datetime import time as day_time

import psycopg
from psycopg.types.json import Jsonb

from zakup import db, matcher, sync
from zakup.goszakup import PORTAL_TZ, Client

ENTITIES = ("announcements", "lots")
ACTIVE = ("queued", "running")


def counts(conn: psycopg.Connection) -> dict[str, int]:
    """Сколько чего в базе. Заказчики и поставщики — из справочника организаций: он
    обновляется каждые 15 минут, поэтому прибавка по ним видна с этой задержкой."""
    announcements, lots, results = conn.execute(
        """SELECT (SELECT count(*) FROM announcements), (SELECT count(*) FROM lots),
                  (SELECT count(*) FROM lot_results)""").fetchone()
    customers, suppliers = conn.execute(
        """SELECT count(*) FILTER (WHERE buys > 0),
                  count(*) FILTER (WHERE wins > 0 OR offers > 0) FROM org_directory""").fetchone()
    size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    return {"announcements": announcements, "lots": lots, "customers": customers,
            "suppliers": suppliers, "results": results, "bytes": size}


def active(conn: psycopg.Connection) -> tuple | None:
    return conn.execute(
        "SELECT id, since, status FROM backfill_runs WHERE status = ANY(%s) ORDER BY id DESC LIMIT 1",
        (list(ACTIVE),)).fetchone()


def start(conn: psycopg.Connection, since: date, user: int | None = None) -> int | None:
    """Новый запуск; None — уже идёт другой."""
    if active(conn):
        return None
    run = conn.execute("INSERT INTO backfill_runs (since, created_by) VALUES (%s, %s) RETURNING id",
                       (since, user)).fetchone()[0]
    conn.commit()
    return run


def stop(conn: psycopg.Connection) -> bool:
    run = active(conn)
    if not run:
        return False
    _finish(conn, run[0], "stopped")
    return True


def _finish(conn: psycopg.Connection, run: int, status: str, error: str | None = None) -> None:
    if status != "failed":
        # Справочник организаций — сразу с догруженным, не дожидаясь круга archive.
        from zakup import orgs

        orgs.refresh_directory(conn)
    conn.execute(
        """UPDATE backfill_runs SET status = %s, finished_at = now(), after = %s, error = %s
           WHERE id = %s""", (status, Jsonb(counts(conn)), error, run))
    conn.commit()


def _running(conn: psycopg.Connection, run: int) -> bool:
    row = conn.execute("SELECT status FROM backfill_runs WHERE id = %s", (run,)).fetchone()
    return bool(row) and row[0] == "running"


def _prepare(conn: psycopg.Connection, client: Client, run: int, since: date) -> bool:
    """Границы на портале и курсоры: от самой старой записи в базе вниз до границы."""
    since_at = datetime.combine(since, day_time.min, PORTAL_TZ)
    first_announcement = sync.find_start(client, "announcements", sync.published_since(since_at))
    first_lot = first_announcement and sync.find_start(
        client, "lots", sync.lot_of_announcement_from(first_announcement))
    if not first_lot:
        _finish(conn, run, "failed", "На портале нет объявлений после этой даты.")
        return False
    upto = conn.execute(
        "SELECT (min(publish_date) AT TIME ZONE 'Asia/Almaty')::date FROM announcements").fetchone()[0]
    bounds = {}
    for entity, floor in zip(ENTITIES, (first_announcement, first_lot), strict=True):
        oldest = conn.execute(f"SELECT min(id) FROM {entity}").fetchone()[0]
        # search_after=x отдаёт записи с id меньше x — то, чего в базе ещё нет.
        top = oldest if oldest is not None and oldest > floor else None
        db.save_state(conn, f"backfill:{entity}", db.SyncState(floor, None, top))
        bounds[entity] = {"floor": floor, "top": top}
    conn.execute(
        """UPDATE backfill_runs SET status = 'running', started_at = now(), bounds = %s, before = %s,
                  upto = %s WHERE id = %s AND status = 'queued'""",
        (Jsonb(bounds), Jsonb(counts(conn)), upto, run))
    conn.commit()
    return True


def step(conn: psycopg.Connection, client: Client, max_pages: int) -> str:
    """Один шаг службы history: до max_pages страниц портала."""
    run = active(conn)
    if not run:
        return "idle"
    run_id, since, status = run
    if status == "queued" and not _prepare(conn, client, run_id, since):
        return "failed"
    keep_going = lambda: _running(conn, run_id)  # noqa: E731 — «Остановить» в настройках
    saved = sync.backfill(conn, client, "announcements", max_pages, keep_going=keep_going)
    if saved is not None:
        return f"announcements {saved}"
    saved = sync.backfill(conn, client, "lots", max_pages, keep_going=keep_going,
                          on_page=lambda ids: matcher.mark_seen(conn, ids))
    if saved is not None:
        return f"lots {saved}"
    if _running(conn, run_id):
        _finish(conn, run_id, "done")
    return "done"


def progress(conn: psycopg.Connection, bounds: dict) -> dict[str, int | None]:
    """Доля пройденного по каждой сущности, в процентах (по id: записи идут плотно)."""
    result = {}
    for entity in ENTITIES:
        b = bounds.get(entity) or {}
        state = db.get_state(conn, f"backfill:{entity}")
        top, floor = b.get("top"), b.get("floor")
        if top is None or state is None or state.resume_after is None:
            result[entity] = 100 if b else None
        else:
            result[entity] = max(0, min(99, int((top - state.resume_after) * 100 / max(1, top - floor))))
    return result


FRESH_PERIODS = (("24 hours", "За 24 часа"), ("7 days", "За 7 дней"), ("30 days", "За 30 дней"))


def fresh(conn: psycopg.Connection, total: dict[str, int]) -> list[dict]:
    """Новое по дате публикации: сколько и примерный объём — по среднему размеру записи
    таблицы (точный подсчёт байтов строк читал бы всю таблицу)."""
    per_row = {table: size / max(1, total[table]) for table, size in conn.execute(
        """SELECT relname, pg_total_relation_size(oid) FROM pg_class
           WHERE relname IN ('announcements', 'lots') AND relnamespace = 'public'::regnamespace""")}
    rows = []
    for interval, label in FRESH_PERIODS:
        announcements, customers, lots = conn.execute(
            """WITH a AS (SELECT id, customer_bin FROM announcements
                          WHERE publish_date >= now() - %s::interval)
               SELECT (SELECT count(*) FROM a), (SELECT count(DISTINCT customer_bin) FROM a),
                      (SELECT count(*) FROM lots l JOIN a ON a.id = l.trd_buy_id)""",
            (interval,)).fetchone()
        size = announcements * float(per_row.get("announcements", 0)) + lots * float(per_row.get("lots", 0))
        rows.append({"label": label, "announcements": announcements, "lots": lots,
                     "customers": customers, "mb": round(size / 1048576)})
    return rows


def overview(conn: psycopg.Connection) -> dict:
    """Для вкладки «Данные» в настройках."""
    # Начало — без единичных выбросов (у портала бывают объявления со старой датой публикации).
    first, last = conn.execute(
        """SELECT percentile_disc(0.001) WITHIN GROUP (ORDER BY publish_date), max(publish_date)
           FROM announcements""").fetchone()
    size = conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
    tables = conn.execute(
        """SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid))
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind IN ('r', 'm')
           ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 6""").fetchall()
    pending_details = conn.execute(
        "SELECT count(*) FROM announcements WHERE detail IS NULL").fetchone()[0]
    now_counts = counts(conn)
    runs = conn.execute(
        """SELECT id, since, status, created_at, started_at, finished_at, bounds, before, after, error,
                  upto, note
           FROM backfill_runs ORDER BY id DESC LIMIT 20""").fetchall()
    last_run = runs[0] if runs else None
    return {
        "first": first, "last": last, "size": size, "tables": tables, "counts": now_counts,
        "pending_details": pending_details, "runs": runs, "fresh": fresh(conn, now_counts), "last_run": last_run,
        "progress": progress(conn, last_run[6]) if last_run and last_run[2] in ACTIVE else None,
    }
