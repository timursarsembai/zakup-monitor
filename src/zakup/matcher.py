"""Подписки: находит новые лоты по запросам пользователей и рассылает уведомления."""

import html
import logging
import time
from typing import NamedTuple

import psycopg

from zakup import telegram

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


def _digests(matches: list[Match]) -> list[tuple[str, list[Match]]]:
    """Склеивает совпадения одного чата в сообщения до MESSAGE_LIMIT символов."""
    digests: list[tuple[str, list[Match]]] = []
    text, batch = "", []
    for match in matches:
        part = format_match(match)
        if batch and len(text) + len(part) + 2 > telegram.MESSAGE_LIMIT:
            digests.append((text, batch))
            text, batch = "", []
        text = f"{text}\n\n{part}" if text else part
        batch.append(match)
    if batch:
        digests.append((text, batch))
    return digests


def _mark_notified(conn: psycopg.Connection, matches: list[Match]) -> None:
    conn.execute(
        """UPDATE matches SET notified_at = now()
           WHERE (subscription_id, lot_id) IN (SELECT * FROM unnest(%s::bigint[], %s::bigint[]))""",
        ([m.subscription_id for m in matches], [m.lot_id for m in matches]),
    )
    conn.commit()


def deliver(conn: psycopg.Connection, pause: float = 1.1) -> int:
    """Рассылает новые совпадения. Возвращает число отправленных совпадений."""
    found = pending(conn, limit=1000)
    # Без подключённого Telegram совпадение видно только на сайте, но считается обработанным.
    _mark_notified(conn, [m for m in found if not m.chat_id])
    by_chat: dict[str, list[Match]] = {}
    for match in found:
        if match.chat_id:
            by_chat.setdefault(match.chat_id, []).append(match)
    sent = 0
    for chat_id, matches in by_chat.items():
        for text, batch in _digests(matches):
            try:
                telegram.send_message(chat_id, text)
            except telegram.TelegramError as error:
                if not error.chat_gone:
                    # Telegram недоступен или лимит: остальное отправим в следующий раз.
                    logger.warning("delivery stopped: %s", error)
                    return sent
                logger.info("chat %s is gone, unlinking", chat_id)
                conn.execute(
                    "UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s",
                    (chat_id,),
                )
                _mark_notified(conn, matches)
                break
            _mark_notified(conn, batch)
            sent += len(batch)
            # Telegram разрешает в один чат около сообщения в секунду.
            time.sleep(pause)
    return sent
