"""Оценки лота (таблица lot_scores) — для фильтров поиска лотов.

Те же цифры, что на странице лота (zakup.lot_insights, zakup.securities), посчитанные
заранее: «цена выше рынка», «повторная закупка», «свой поставщик», надёжность заказчика,
сколько денег заморозить. Служба scores считает открытые лоты: сначала те, у которых оценок
ещё нет, потом устаревшие (старше суток или после разбора процентов обеспечения).
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

import psycopg

from zakup import lot_insights, securities

logger = logging.getLogger(__name__)

STALE = "1 day"
THROTTLE = 1.0  # тесты ставят 0
BATCH = 300

COLUMNS = ("market_diff", "market_median", "market_n", "repeats", "repeats_failed", "same_winner_bin",
           "same_winner_name", "customer_failed_share", "customer_results", "bid_deposit",
           "contract_deposit", "freeze_total", "dumping_floor")


def compute(conn: psycopg.Connection, lot_id: int, customers: dict | None = None) -> dict[str, Any] | None:
    facts = lot_insights.lot_facts(conn, lot_id)
    if facts is None:
        return None
    market = lot_insights.market(conn, lot_id, facts)
    repeats = lot_insights.repeats(conn, lot_id, facts)
    history = lot_insights.history(conn, lot_id, facts)
    same = history["same_winner"] if history else None
    # Заказчик один на много лотов — за проход считаем его один раз.
    cache = customers if customers is not None else {}
    if facts["customer"] not in cache:
        cache[facts["customer"]] = lot_insights.customer(conn, facts)
    customer = cache[facts["customer"]]
    deposits = securities.for_lot(facts["securities"], facts["method"], facts["amount"], facts["prepayment"],
                                  subject=facts["subject"])
    bid = deposits["bid"] if deposits else None
    contract = deposits["contract_max"] if deposits else None
    return {
        "market_diff": market["diff"] if market else None,
        "market_median": market["median"] if market else None,
        "market_n": market["n"] if market else None,
        "repeats": len(repeats["rows"]) if repeats else 0,
        "repeats_failed": repeats["failed"] if repeats else 0,
        "same_winner_bin": same["bin"] if same else None,
        "same_winner_name": same["name"] if same else None,
        "customer_failed_share": customer["failed_share"] if customer else None,
        "customer_results": customer["results"] if customer else None,
        "bid_deposit": bid, "contract_deposit": contract,
        "freeze_total": (bid or 0) + (contract or 0) if deposits else None,
        "dumping_floor": deposits["dumping"]["floor"] if deposits and deposits["dumping"] else None,
    }


def save(conn: psycopg.Connection, lot_id: int, values: dict[str, Any]) -> None:
    names = ", ".join(COLUMNS)
    conn.execute(
        f"""INSERT INTO lot_scores (lot_id, {names}, computed_at)
            VALUES (%(lot_id)s, {", ".join(f"%({c})s" for c in COLUMNS)}, now())
            ON CONFLICT (lot_id) DO UPDATE SET {", ".join(f"{c} = excluded.{c}" for c in COLUMNS)},
                computed_at = now()""", {**values, "lot_id": lot_id})


def refresh(conn: psycopg.Connection, limit: int = BATCH) -> int:
    """Считает очередную порцию открытых лотов; возвращает, сколько посчитано."""
    rows = conn.execute(
        """SELECT l.id FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
           LEFT JOIN lot_scores s ON s.lot_id = l.id
           WHERE a.end_date > now() AND a.lots_ready
             AND (s.lot_id IS NULL OR s.computed_at < now() - %s::interval
                  OR s.computed_at < a.securities_at)
           ORDER BY s.lot_id IS NOT NULL, s.computed_at NULLS FIRST, a.end_date
           LIMIT %s""", (STALE, limit)).fetchall()
    conn.commit()
    customers: dict = {}
    done = 0
    for (lot_id,) in rows:
        started = time.monotonic()
        try:
            values = compute(conn, lot_id, customers)
            if values is not None:
                save(conn, lot_id, values)
            conn.commit()
            done += 1
        except Exception as exc:  # noqa: BLE001 — один плохой лот не должен останавливать службу
            conn.rollback()
            logger.warning("scores for lot %s failed: %s", lot_id, exc)
        # Не больше половины ядра базы: после каждого лота пауза, равная времени расчёта.
        time.sleep(min(time.monotonic() - started, 2) * THROTTLE)
    return done


def run(database_url: str, loop: bool) -> int:
    from zakup import db

    while True:
        with db.connect(database_url) as conn:
            started = datetime.now(UTC)
            done = refresh(conn)
        print(f"scores: {done} in {(datetime.now(UTC) - started).total_seconds():.0f}s", flush=True)
        if not loop:
            return 0
        if done < BATCH:  # очередь пуста — проверим через минуту
            time.sleep(60)
