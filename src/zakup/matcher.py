"""Подписки: находит совпадения и рассылает уведомления.

Два вида подписок: 'lots' — слова среди новых лотов; 'announcements' —
сохранённый фильтр объявлений (zakup.filters), объявление приходит один раз,
когда впервые подошло под фильтр.
"""

import html
import logging
import time
from datetime import UTC, datetime
from typing import NamedTuple

import psycopg

from zakup import telegram
from zakup.filters import REGIONS, Filters
from zakup.goszakup import PORTAL_TZ

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
           WHERE s.active AND s.kind = 'lots'
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


def run_filters(conn: psycopg.Connection, now: datetime | None = None) -> int:
    """Отмечает объявления, подошедшие под фильтры подписок. Возвращает число новых."""
    now = now or datetime.now(UTC)
    found = 0
    subscriptions = conn.execute(
        "SELECT id, filters FROM subscriptions WHERE active AND kind = 'announcements'"
    ).fetchall()
    for sub_id, data in subscriptions:
        where, params = Filters.from_json(data).where(now)
        found += conn.execute(
            f"""INSERT INTO announcement_matches (subscription_id, announcement_id)
                SELECT %(sub_id)s, a.id FROM announcements a WHERE {where}
                ON CONFLICT DO NOTHING""",
            {**params, "sub_id": sub_id},
        ).rowcount
    conn.commit()
    return found


class Notice(NamedTuple):
    table: str
    subscription_id: int
    item_id: int
    chat_id: str | None
    text: str


def _lot_notices(conn: psycopg.Connection, limit: int) -> list[Notice]:
    return [
        Notice("matches", m.subscription_id, m.lot_id, m.chat_id, format_match(m))
        for m in pending(conn, limit)
    ]


def _announcement_notices(conn: psycopg.Connection, limit: int) -> list[Notice]:
    rows = conn.execute(
        """SELECT s.id, s.name, u.telegram_chat_id, a.id, a.number_anno, a.name_ru,
                  a.total_sum, a.end_date, a.prepayment_max, a.kato,
                  coalesce(a.customer_name_ru, a.org_name_ru)
           FROM announcement_matches m
           JOIN subscriptions s ON s.id = m.subscription_id
           JOIN users u ON u.id = s.user_id
           JOIN announcements a ON a.id = m.announcement_id
           WHERE m.notified_at IS NULL
           ORDER BY s.id, a.prepayment_max DESC NULLS LAST, a.end_date
           LIMIT %s""",
        (limit,),
    ).fetchall()
    return [
        Notice("announcement_matches", r[0], r[3], r[2], format_announcement(r[1], *r[3:]))
        for r in rows
    ]


def format_announcement(
    subscription_name, announcement_id, number, name, total_sum, end_date, prepayment, kato,
    customer,
) -> str:
    lines = [
        f"<b>{html.escape(subscription_name)}</b>",
        f"<b>№ {html.escape(number or str(announcement_id))}</b> {html.escape(name or '')}",
    ]
    money = f"{total_sum:,.2f} ₸".replace(",", " ") if total_sum is not None else ""
    if prepayment:
        money += f" · <b>аванс {float(prepayment):g}%</b>"
    if money:
        lines.append(money.strip(" ·"))
    if end_date:
        lines.append(f"Окончание приема: {end_date.astimezone(PORTAL_TZ):%d.%m.%Y %H:%M}")
    regions = sorted({REGIONS[k[:2]] for k in kato or [] if k[:2] in REGIONS})
    if regions:
        lines.append("Место поставки: " + ", ".join(regions))
    if customer:
        lines.append(f"Заказчик: {html.escape(customer)}")
    lines.append(PORTAL_ANNOUNCEMENT_URL.format(announcement_id))
    return "\n".join(lines)


def _digests(notices: list[Notice]) -> list[tuple[str, list[Notice]]]:
    """Склеивает уведомления одного чата в сообщения до MESSAGE_LIMIT символов."""
    digests: list[tuple[str, list[Notice]]] = []
    text, batch = "", []
    for notice in notices:
        if batch and len(text) + len(notice.text) + 2 > telegram.MESSAGE_LIMIT:
            digests.append((text, batch))
            text, batch = "", []
        text = f"{text}\n\n{notice.text}" if text else notice.text
        batch.append(notice)
    if batch:
        digests.append((text, batch))
    return digests


def _mark_notified(conn: psycopg.Connection, notices: list[Notice]) -> None:
    for table, column in (("matches", "lot_id"), ("announcement_matches", "announcement_id")):
        chosen = [n for n in notices if n.table == table]
        if chosen:
            conn.execute(
                f"""UPDATE {table} SET notified_at = now()
                    WHERE (subscription_id, {column}) IN
                          (SELECT * FROM unnest(%s::bigint[], %s::bigint[]))""",
                ([n.subscription_id for n in chosen], [n.item_id for n in chosen]),
            )
    conn.commit()


def deliver(conn: psycopg.Connection, pause: float = 1.1) -> int:
    """Рассылает новые совпадения. Возвращает число отправленных."""
    found = _announcement_notices(conn, 500) + _lot_notices(conn, 1000)
    # Без подключённого Telegram совпадение видно только на сайте, но считается обработанным.
    _mark_notified(conn, [n for n in found if not n.chat_id])
    by_chat: dict[str, list[Notice]] = {}
    for notice in found:
        if notice.chat_id:
            by_chat.setdefault(notice.chat_id, []).append(notice)
    sent = 0
    for chat_id, notices in by_chat.items():
        for text, batch in _digests(notices):
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
                _mark_notified(conn, notices)
                break
            _mark_notified(conn, batch)
            sent += len(batch)
            # Telegram разрешает в один чат около сообщения в секунду.
            time.sleep(pause)
    return sent
