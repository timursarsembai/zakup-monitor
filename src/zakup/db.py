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


def upsert_lot(conn: psycopg.Connection, item: dict[str, Any], overwrite: bool = True) -> None:
    """overwrite=False — только завести отсутствующий лот (из карточки объявления): её
    сокращённый ответ не должен затирать полный из REST — они бы переписывали друг друга."""
    temp = _temp_twin(conn, item)
    if not overwrite:
        if conn.execute("SELECT 1 FROM lots WHERE id = %s", (int(item["id"]),)).fetchone():
            if temp:
                adopt_temp_lot(conn, temp, int(item["id"]))
            return
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
    # Новый лот — сразу со списками кодов для фильтров (миграция 0019): место поставки —
    # объявления, пока подробности не дали своё. Не известно — оставляем NULL (не пустой
    # список), чтобы заполнили позже подробности или `zakup codes`.
    conn.execute(
        """UPDATE lots l SET regions = nullif(codes2(coalesce(nullif(l.kato, '{}'),
                                (SELECT a.kato FROM announcements a WHERE a.id = l.trd_buy_id))), '{}'),
                             divisions = nullif(codes2(l.enstru), '{}')
           WHERE l.id = %s AND l.regions IS NULL""",
        (int(item["id"]),))
    if temp:
        adopt_temp_lot(conn, temp, int(item["id"]))


# Лоты со страниц старого портала (zakup.oldportal) — номера OWS у них нет, номер временный:
# TEMP_LOT_BASE + номер лота портала. Настоящие номера OWS и платформы много меньше.
TEMP_LOT_BASE = 9_000_000_000_000
# Что ссылается на лот: таблица → столбцы ключа кроме lot_id (None — ключа с лотом нет).
_LOT_REFS: dict[str, tuple[str, ...] | None] = {
    "matches": ("subscription_id",), "lot_marks": ("user_id",), "chat_lots": ("chat_id",),
    "seller_alerts": ("seller_id",), "supplier_questions": ("url",), "tech_proposals": ("url",),
    "lot_specs": (), "bid_advice": (), "lot_results": (), "spec_summaries": (), "lot_scores": (),
    "lot_research": None, "search_ledger": None,
}
# Разобранное из подробностей (место поставки, аванс, цена…) — у лота из REST этого нет.
_DETAIL_COLUMNS = ("prepayment", "kato", "price", "unit_name", "extra_desc", "supply_date", "enstru",
                   "spec_url", "unit_name_kz", "extra_desc_kz")


def _temp_twin(conn: psycopg.Connection, item: dict[str, Any]) -> int | None:
    """Временный лот с тем же номером в том же объявлении — для лота OWS. Есть — сразу
    блокируем объявление: порядок блокировок как в details.save и oldportal.fill (сначала
    объявление, потом лоты), иначе встречная запись подробностей даст DeadlockDetected."""
    aid = _int(item.get("trd_buy_id"))
    if int(item["id"]) >= TEMP_LOT_BASE or not item.get("lot_number") or not aid:
        return None
    row = conn.execute("SELECT id FROM lots WHERE trd_buy_id = %s AND lot_number = %s AND id >= %s",
                       (aid, item["lot_number"], TEMP_LOT_BASE)).fetchone()
    if row:
        conn.execute("SELECT 1 FROM announcements WHERE id = %s FOR UPDATE", (aid,))
    return row[0] if row else None


def _forget_lots(conn: psycopg.Connection, ids: list[int]) -> None:
    """Строки про лоты, у которых нет внешнего ключа (каскад их не уберёт), и сами лоты."""
    for table in _LOT_REFS:
        conn.execute(f"DELETE FROM {table} WHERE lot_id = ANY(%s)", (ids,))
    conn.execute("DELETE FROM lots WHERE id = ANY(%s)", (ids,))


def adopt_temp_lot(conn: psycopg.Connection, temp_id: int, real_id: int) -> None:
    """Лот пришёл из OWS — временный (со старого портала) заменяем им: избранное, подборы,
    отметки подписок («уже присылали» — второго уведомления не будет) переходят к настоящему;
    где у настоящего уже своё — остаётся его. Разобранное из подробностей (место поставки,
    аванс) — настоящему, пока своих нет. Старая ссылка /lots/<временный> ведёт на новый."""
    for table, keys in _LOT_REFS.items():
        if keys is None:
            conn.execute(f"UPDATE {table} SET lot_id = %s WHERE lot_id = %s", (real_id, temp_id))
            continue
        same = "".join(f" AND x.{k} = t.{k}" for k in keys)
        conn.execute(
            f"""UPDATE {table} t SET lot_id = %(real)s WHERE t.lot_id = %(temp)s
                AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.lot_id = %(real)s{same})""",
            {"real": real_id, "temp": temp_id})
    for table in ("match_pages", "product_catalog"):
        column = "lot_ids" if table == "match_pages" else "lots"
        conn.execute(f"UPDATE {table} SET {column} = array_replace({column}, %s, %s) WHERE %s = ANY({column})",
                     (temp_id, real_id, temp_id))
    # Регионы и разделы у лота из REST — грубые (по объявлению): точные — от временного.
    copy = ", ".join([f"{c} = coalesce(r.{c}, t.{c})" for c in _DETAIL_COLUMNS]
                     + [f"{c} = CASE WHEN r.kato IS NULL THEN t.{c} ELSE r.{c} END" for c in ("regions", "divisions")])
    conn.execute(f"UPDATE lots r SET {copy} FROM lots t WHERE r.id = %s AND t.id = %s", (real_id, temp_id))
    # Подробности объявления (по ним страницы лотов) ещё со временным — пока OWS не прислал свои.
    conn.execute(
        """UPDATE announcements a SET detail = jsonb_set(a.detail, '{Lots}', (
               SELECT jsonb_agg(CASE WHEN x->>'id' = %(temp)s::text
                                     THEN jsonb_set(x, '{id}', to_jsonb(%(real)s::bigint)) ELSE x END ORDER BY o)
               FROM jsonb_array_elements(a.detail->'Lots') WITH ORDINALITY e(x, o)))
           WHERE a.id = (SELECT trd_buy_id FROM lots WHERE id = %(temp)s)
             AND jsonb_typeof(a.detail->'Lots') = 'array' AND jsonb_array_length(a.detail->'Lots') > 0""",
        {"real": real_id, "temp": temp_id})
    conn.execute("""INSERT INTO lot_aliases (old_id, new_id, announcement_id)
                    SELECT %(temp)s, %(real)s, trd_buy_id FROM lots WHERE id = %(temp)s
                    ON CONFLICT (old_id) DO UPDATE SET new_id = excluded.new_id""",
                 {"temp": temp_id, "real": real_id})
    _forget_lots(conn, [temp_id])


def drop_temp_lots(conn: psycopg.Connection, announcement_id: int) -> int:
    """Временные лоты объявления, которым в OWS не нашлось пары (лот убрали), — удаляем:
    подробности OWS с лотами пришли, они главные. Ссылка на такой лот ведёт на объявление."""
    ids = [r[0] for r in conn.execute("SELECT id FROM lots WHERE trd_buy_id = %s AND id >= %s",
                                      (announcement_id, TEMP_LOT_BASE))]
    if ids:
        conn.execute("""INSERT INTO lot_aliases (old_id, announcement_id) SELECT unnest(%s::bigint[]), %s
                        ON CONFLICT (old_id) DO NOTHING""", (ids, announcement_id))
        _forget_lots(conn, ids)
    return len(ids)


UPSERT = {"announcements": upsert_announcement, "lots": upsert_lot}
