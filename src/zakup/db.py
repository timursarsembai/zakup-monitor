from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Any, NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from zakup.goszakup import parse_date


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url)


def migrate(conn: psycopg.Connection) -> None:
    conn.execute(files("zakup").joinpath("schema.sql").read_text(encoding="utf-8"))
    conn.commit()


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
               fetched_at = now()""",
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
               fetched_at = now()""",
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
