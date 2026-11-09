import time
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Any, NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from zakup.goszakup import parse_date


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url)


# Изменения базы — нумерованные файлы migrations/NNNN_что.sql; каждый применяется один
# раз, применённые записаны в schema_migrations. Новое изменение — новый файл с
# следующим номером; старые файлы не правим.
BASELINE = "0001_initial"


def migrations() -> list[tuple[str, str]]:
    """[(имя без .sql, текст)] по порядку номеров."""
    folder = files("zakup").joinpath("migrations")
    return sorted((f.name.removesuffix(".sql"), f.read_text(encoding="utf-8"))
                  for f in folder.iterdir() if f.name.endswith(".sql"))


def applied(conn: psycopg.Connection) -> set[str]:
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                        name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
    done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
    if not done and conn.execute("SELECT to_regclass('schema_state') IS NOT NULL").fetchone()[0]:
        # База создана прежним способом (весь schema.sql целиком) — первая миграция уже в ней.
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (BASELINE,))
        done = {BASELINE}
    conn.commit()
    return done


def pending(conn: psycopg.Connection) -> list[str]:
    done = applied(conn)
    return [name for name, _ in migrations() if name not in done]


MIGRATE_LOCK = 7_403


def migrate(conn: psycopg.Connection, attempts: int = 10) -> bool:
    """Применяет новые миграции. True — что-то применилось.

    Команды ALTER/CREATE берут на таблицу исключительную блокировку. Сборщики в это
    время пишут в те же таблицы, и раньше это давало взаимные блокировки
    (DeadlockDetected). Поэтому без новых миграций ничего не выполняется, а при новых
    lock_timeout меньше deadlock_timeout базы (1 с) — миграция уступает первой и
    повторяет, сборщик не падает.
    """
    done = applied(conn)
    todo = [(name, sql) for name, sql in migrations() if name not in done]
    for name, sql in todo:
        for attempt in range(attempts):
            try:
                conn.execute("SET lock_timeout = '500ms'")
                # Сайт и сборщик могут начать одновременно: миграции — по одной, и
                # уже применённую другим процессом не повторяем.
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
                if conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (name,)).fetchone():
                    conn.execute("RESET lock_timeout")
                    conn.commit()
                    break
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
                conn.execute("RESET lock_timeout")
                conn.commit()
                break
            except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected):
                conn.rollback()
                if attempt == attempts - 1:
                    raise
                time.sleep(1 + attempt)
    return bool(todo)


class SyncState(NamedTuple):
    last_id: int
    walk_top: int | None
    resume_after: int | None


def get_state(conn: psycopg.Connection, entity: str) -> SyncState | None:
    row = conn.execute(
        "SELECT last_id, walk_top, resume_after FROM sync_state WHERE entity = %s", (entity,)
    ).fetchone()
    return SyncState(*row) if row else None


def save_state(conn: psycopg.Connection, entity: str, state: SyncState) -> None:
    conn.execute(
        """INSERT INTO sync_state (entity, last_id, walk_top, resume_after)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (entity) DO UPDATE SET last_id = excluded.last_id,
               walk_top = excluded.walk_top, resume_after = excluded.resume_after,
               updated_at = now()""",
        (entity, *state),
    )


def _number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def upsert_announcement(conn: psycopg.Connection, item: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO announcements (
               id, number_anno, name_ru, name_kz, total_sum, count_lots, trade_method_id,
               status_id, customer_bin, customer_name_ru, org_bin, org_name_ru,
               publish_date, start_date, end_date, last_update_date, raw)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET
               number_anno = excluded.number_anno, name_ru = excluded.name_ru,
               name_kz = excluded.name_kz, total_sum = excluded.total_sum,
               -- В списке этих полей нет, они приходят только из карточки.
               count_lots = coalesce(excluded.count_lots, announcements.count_lots),
               customer_bin = coalesce(excluded.customer_bin, announcements.customer_bin),
               customer_name_ru = coalesce(excluded.customer_name_ru, announcements.customer_name_ru),
               trade_method_id = excluded.trade_method_id, status_id = excluded.status_id,
               org_bin = excluded.org_bin,
               org_name_ru = excluded.org_name_ru, publish_date = excluded.publish_date,
               start_date = excluded.start_date, end_date = excluded.end_date,
               last_update_date = excluded.last_update_date, raw = excluded.raw,
               fetched_at = now()
           -- Портал не менял объявление — строку не переписываем: пересканирование (10 000
           -- за круг) иначе плодит старые версии строк и раздувает таблицу.
           WHERE announcements.raw IS DISTINCT FROM excluded.raw""",
        (
            int(item["id"]),
            item.get("number_anno"),
            item.get("name_ru"),
            item.get("name_kz"),
            _number(item.get("total_sum")),
            _int(item.get("count_lots")),
            _int(item.get("ref_trade_methods_id")),
            _int(item.get("ref_buy_status_id")),
            item.get("customer_bin"),
            item.get("customer_name_ru"),
            item.get("org_bin"),
            item.get("org_name_ru"),
            parse_date(item.get("publish_date")),
            parse_date(item.get("start_date")),
            parse_date(item.get("end_date")),
            parse_date(item.get("last_update_date")),
            Jsonb(item),
        ),
    )


def upsert_lot(conn: psycopg.Connection, item: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO lots (
               id, trd_buy_id, number_anno, lot_number, name_ru, name_kz, description_ru,
               amount, count, status_id, trade_method_id, customer_bin, customer_name_ru,
               last_update_date, raw)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET
               trd_buy_id = excluded.trd_buy_id, number_anno = excluded.number_anno,
               lot_number = excluded.lot_number, name_ru = excluded.name_ru,
               name_kz = excluded.name_kz, description_ru = excluded.description_ru,
               amount = excluded.amount, count = excluded.count,
               status_id = excluded.status_id, trade_method_id = excluded.trade_method_id,
               customer_bin = excluded.customer_bin,
               customer_name_ru = excluded.customer_name_ru,
               last_update_date = excluded.last_update_date, raw = excluded.raw,
               fetched_at = now()
           WHERE lots.raw IS DISTINCT FROM excluded.raw""",
        (
            int(item["id"]),
            _int(item.get("trd_buy_id")),
            item.get("trd_buy_number_anno"),
            item.get("lot_number"),
            item.get("name_ru"),
            item.get("name_kz"),
            item.get("description_ru"),
            _number(item.get("amount")),
            _number(item.get("count")),
            _int(item.get("ref_lot_status_id")),
            _int(item.get("ref_trade_methods_id")),
            item.get("customer_bin"),
            item.get("customer_name_ru"),
            parse_date(item.get("last_update_date")),
            Jsonb(item),
        ),
    )


UPSERT = {"announcements": upsert_announcement, "lots": upsert_lot}
