"""Подписки: находит подходящие лоты и рассылает уведомления.

Совпадение — всегда лот (таблица matches), потому что заявку подают по лоту.
Два вида подписок: 'lots' — слова среди новых лотов; 'announcements' —
сохранённый фильтр (zakup.filters). В Telegram лоты одного объявления
приходят одним блоком со ссылкой на страницу объявления на нашем сайте.
"""

import html
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

import psycopg

from zakup import SITE_URL, telegram
from zakup.filters import REGIONS, Filters
from zakup.goszakup import PORTAL_TZ

logger = logging.getLogger(__name__)

PORTAL_ANNOUNCEMENT_URL = "https://goszakup.gov.kz/ru/announce/index/{}"
# В одном блоке уведомления не больше стольких лотов, остальные — на сайте.
LOTS_PER_BLOCK = 8


def announcement_url(announcement_id: int) -> str:
    return f"{SITE_URL}/a/{announcement_id}"


def lot_url(lot_id: int) -> str:
    return f"{SITE_URL}/lots/{lot_id}"


def run(conn: psycopg.Connection) -> int:
    """Подписки на слова: лоты, пришедшие после прошлого запуска."""
    max_seq = conn.execute("SELECT coalesce(max(seen_seq), 0) FROM lots").fetchone()[0]
    found = conn.execute(
        """INSERT INTO matches (subscription_id, lot_id)
           SELECT s.id, l.id
           FROM subscriptions s
           JOIN lots l ON l.seen_seq > s.matched_up_to AND l.seen_seq <= %(max_seq)s
           WHERE s.active AND s.kind = 'lots'
             AND l.tsv @@ websearch_to_tsquery(
                 'russian', regexp_replace(s.query, '\\s+или\\s+', ' or ', 'gi'))
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


def run_filters(conn: psycopg.Connection, now: datetime | None = None) -> int:
    """Подписки-фильтры: отмечает лоты, впервые подошедшие под фильтр."""
    now = now or datetime.now(UTC)
    found = 0
    subscriptions = conn.execute(
        "SELECT id, filters FROM subscriptions WHERE active AND kind = 'announcements'"
    ).fetchall()
    for sub_id, data in subscriptions:
        where, params = Filters.from_json(data).where(now)
        found += conn.execute(
            f"""INSERT INTO matches (subscription_id, lot_id)
                SELECT %(sub_id)s, l.id FROM lots l, announcements a WHERE {where}
                ON CONFLICT DO NOTHING""",
            {**params, "sub_id": sub_id},
        ).rowcount
    conn.commit()
    return found


class PendingLot(NamedTuple):
    subscription_id: int
    subscription_name: str
    chat_id: str | None
    lot_id: int
    lot_name: str | None
    lot_description: str | None
    amount: Decimal | None
    count: Decimal | None
    unit_name: str | None
    prepayment: Decimal | None
    lot_kato: list[str] | None
    announcement_id: int | None
    number_anno: str | None
    announcement_name: str | None
    end_date: datetime | None
    customer: str | None
    announcement_kato: list[str] | None


def pending(conn: psycopg.Connection, limit: int = 2000) -> list[PendingLot]:
    rows = conn.execute(
        """SELECT s.id, s.name, u.telegram_chat_id, l.id, l.name_ru,
                  coalesce(l.extra_desc, l.description_ru), l.amount, l.count, l.unit_name,
                  l.prepayment, l.kato, l.trd_buy_id, coalesce(a.number_anno, l.number_anno),
                  a.name_ru, a.end_date,
                  coalesce(a.customer_name_ru, l.customer_name_ru, a.org_name_ru), a.kato
           FROM matches m
           JOIN subscriptions s ON s.id = m.subscription_id
           JOIN users u ON u.id = s.user_id
           JOIN lots l ON l.id = m.lot_id
           LEFT JOIN announcements a ON a.id = l.trd_buy_id
           WHERE m.notified_at IS NULL
           ORDER BY s.id, a.end_date NULLS LAST, l.trd_buy_id, l.prepayment DESC NULLS LAST, l.id
           LIMIT %s""",
        (limit,),
    ).fetchall()
    return [PendingLot(*row) for row in rows]


def _money(value) -> str:
    return f"{value:,.2f}".replace(",", " ") + " ₸"


def _quantity(value: Decimal) -> str:
    text = f"{value.normalize():f}" if isinstance(value, Decimal) else f"{value:g}"
    return text.replace(".", ",")


def _regions(kato) -> str:
    return ", ".join(sorted({REGIONS[k[:2]] for k in kato or [] if k[:2] in REGIONS}))


def format_lot(lot: PendingLot) -> str:
    title = html.escape(lot.lot_name or "Без названия")
    if lot.lot_description and lot.lot_description != lot.lot_name:
        title += f" — {html.escape(lot.lot_description[:120])}"
    parts = [title]
    if lot.count is not None and lot.unit_name:
        parts.append(f"{_quantity(lot.count)} {html.escape(lot.unit_name.lower())}")
    if lot.amount is not None:
        parts.append(_money(lot.amount))
    if lot.prepayment:
        parts.append(f"<b>аванс {float(lot.prepayment):g}%</b>")
    if region := _regions(lot.lot_kato or lot.announcement_kato):
        parts.append(region)
    return "• " + " · ".join(parts)


def format_block(lots: list[PendingLot]) -> str:
    """Блок одного объявления: шапка и подошедшие лоты."""
    first = lots[0]
    lines = [f"<b>{html.escape(first.announcement_name or first.lot_name or 'Объявление')}</b>"]
    head = [f"№ {html.escape(first.number_anno or str(first.announcement_id or ''))}"]
    if first.end_date:
        head.append(f"приём до {first.end_date.astimezone(PORTAL_TZ):%d.%m %H:%M}")
    lines.append(" · ".join(head))
    if first.customer:
        lines.append(html.escape(first.customer))
    lines += [format_lot(lot) for lot in lots[:LOTS_PER_BLOCK]]
    if len(lots) > LOTS_PER_BLOCK:
        lines.append(f"…и ещё {len(lots) - LOTS_PER_BLOCK} лотов")
    if first.announcement_id:
        lines.append(announcement_url(first.announcement_id))
    return "\n".join(lines)


class Notice(NamedTuple):
    chat_id: str | None
    subscription_name: str
    text: str
    keys: list[tuple[int, int]]  # (subscription_id, lot_id)


def notices(found: list[PendingLot]) -> list[Notice]:
    groups: dict[tuple, list[PendingLot]] = {}
    for lot in found:
        groups.setdefault((lot.subscription_id, lot.announcement_id or -lot.lot_id), []).append(lot)
    return [
        Notice(
            lots[0].chat_id,
            lots[0].subscription_name,
            format_block(lots),
            [(lot.subscription_id, lot.lot_id) for lot in lots],
        )
        for lots in groups.values()
    ]


def _digests(chat_notices: list[Notice]) -> list[tuple[str, list[Notice]]]:
    """Склеивает блоки одного чата в сообщения до MESSAGE_LIMIT символов.

    Перед блоками каждой подписки — её название.
    """
    digests: list[tuple[str, list[Notice]]] = []
    text, batch, current = "", [], None
    for notice in chat_notices:
        part = notice.text
        if notice.subscription_name != current or not batch:
            part = f"🔔 <b>{html.escape(notice.subscription_name)}</b>\n\n{part}"
        if batch and len(text) + len(part) + 2 > telegram.MESSAGE_LIMIT:
            digests.append((text, batch))
            text, batch = "", []
            part = f"🔔 <b>{html.escape(notice.subscription_name)}</b>\n\n{notice.text}"
        current = notice.subscription_name
        text = f"{text}\n\n{part}" if text else part
        batch.append(notice)
    if batch:
        digests.append((text, batch))
    return digests


def _mark_notified(conn: psycopg.Connection, chosen: list[Notice]) -> None:
    keys = [key for notice in chosen for key in notice.keys]
    if keys:
        conn.execute(
            """UPDATE matches SET notified_at = now()
               WHERE (subscription_id, lot_id) IN
                     (SELECT * FROM unnest(%s::bigint[], %s::bigint[]))""",
            ([k[0] for k in keys], [k[1] for k in keys]),
        )
    conn.commit()


def deliver(conn: psycopg.Connection, pause: float = 1.1) -> int:
    """Рассылает новые совпадения. Возвращает число отправленных лотов."""
    found = notices(pending(conn))
    # Без подключённого Telegram совпадение видно только на сайте, но считается обработанным.
    _mark_notified(conn, [n for n in found if not n.chat_id])
    by_chat: dict[str, list[Notice]] = {}
    for notice in found:
        if notice.chat_id:
            by_chat.setdefault(notice.chat_id, []).append(notice)
    sent = 0
    for chat_id, chat_notices in by_chat.items():
        for text, batch in _digests(chat_notices):
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
                _mark_notified(conn, chat_notices)
                break
            _mark_notified(conn, batch)
            sent += sum(len(n.keys) for n in batch)
            # Telegram разрешает в один чат около сообщения в секунду.
            time.sleep(pause)
    return sent
