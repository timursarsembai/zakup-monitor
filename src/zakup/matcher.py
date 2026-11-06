"""Подписки: находит новые лоты по запросам пользователей и рассылает уведомления."""

import html
import logging
from typing import NamedTuple

import psycopg

from zakup import notify

logger = logging.getLogger(__name__)

PORTAL_ANNOUNCEMENT_URL = "https://goszakup.gov.kz/ru/announce/index/{}"


class Match(NamedTuple):
    subscription_id: int
    subscription_name: str
    chat_id: str | None
    lot_id: int
    trd_buy_id: int | None
    lot_name: str | None
    amount: object
    customer_name: str | None


def run(conn: psycopg.Connection) -> int:
    """Сопоставляет лоты, пришедшие после прошлого запуска. Возвращает число совпадений."""
    max_seq = conn.execute("SELECT coalesce(max(seen_seq), 0) FROM lots").fetchone()[0]
    found = conn.execute(
        """INSERT INTO matches (subscription_id, lot_id)
           SELECT s.id, l.id
           FROM subscriptions s
           JOIN lots l ON l.seen_seq > s.matched_up_to AND l.seen_seq <= %(max_seq)s
           WHERE s.active
             AND l.tsv @@ websearch_to_tsquery('russian', s.query)
             AND (s.min_amount IS NULL OR l.amount >= s.min_amount)
             AND (s.max_amount IS NULL OR l.amount <= s.max_amount)
             AND (s.customer_bins IS NULL OR l.customer_bin = ANY (s.customer_bins))
           ON CONFLICT DO NOTHING""",
        {"max_seq": max_seq},
    ).rowcount
    conn.execute(
        # И для выключенных: после включения они не должны прислать накопившееся.
        "UPDATE subscriptions SET matched_up_to = %s WHERE matched_up_to < %s",
        (max_seq, max_seq),
    )
    conn.commit()
    return found


def pending(conn: psycopg.Connection, limit: int = 200) -> list[Match]:
    rows = conn.execute(
        """SELECT s.id, s.name, u.telegram_chat_id, l.id, l.trd_buy_id, l.name_ru,
                  l.amount, l.customer_name_ru
           FROM matches m
           JOIN subscriptions s ON s.id = m.subscription_id
           JOIN users u ON u.id = s.user_id
           JOIN lots l ON l.id = m.lot_id
           WHERE m.notified_at IS NULL
           ORDER BY m.matched_at, l.id
           LIMIT %s""",
        (limit,),
    ).fetchall()
    return [Match(*row) for row in rows]


def format_match(match: Match) -> str:
    lines = [
        f"<b>{html.escape(match.subscription_name)}</b>",
        html.escape(match.lot_name or "Без названия"),
    ]
    if match.amount is not None:
        lines.append(f"Сумма: {match.amount:,.2f} ₸".replace(",", " "))
    if match.customer_name:
        lines.append(f"Заказчик: {html.escape(match.customer_name)}")
    if match.trd_buy_id:
        lines.append(PORTAL_ANNOUNCEMENT_URL.format(match.trd_buy_id))
    return "\n".join(lines)


def deliver(conn: psycopg.Connection) -> int:
    sent = 0
    for match in pending(conn):
        if match.chat_id and not notify.send(format_match(match), chat_id=match.chat_id):
            # Telegram недоступен: остальное отправим в следующий раз.
            break
        # Без chat_id совпадение остаётся только в вебе, но тоже считается обработанным.
        conn.execute(
            "UPDATE matches SET notified_at = now() WHERE subscription_id = %s AND lot_id = %s",
            (match.subscription_id, match.lot_id),
        )
        conn.commit()
        sent += 1
    return sent
