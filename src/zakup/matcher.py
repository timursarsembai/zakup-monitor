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

from zakup import SITE_URL, bot, telegram
from zakup.auth import ENTITLED_SQL
from zakup.filters import REGIONS, Filters
from zakup.goszakup import PORTAL_TZ

logger = logging.getLogger(__name__)

PORTAL_ANNOUNCEMENT_URL = "https://goszakup.gov.kz/ru/announce/index/{}"
# В одном блоке уведомления не больше стольких лотов, остальные — на сайте.
LOTS_PER_BLOCK = 6


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
        "SELECT id, filters, scope FROM subscriptions WHERE active AND kind = 'announcements'"
    ).fetchall()
    for sub_id, data, scope in subscriptions:
        try:
            found += insert_matches(conn, sub_id, Filters.from_json(data), scope, now)
            conn.commit()
        except (psycopg.Error, KeyError, ValueError) as error:
            # Одна сломанная подписка не должна останавливать остальные и рассылку.
            conn.rollback()
            logger.warning("subscription %s skipped: %s", sub_id, error)
    return found


def insert_matches(conn: psycopg.Connection, sub_id: int, current: Filters, scope: str,
                   now: datetime, notified: bool = False) -> int:
    where, params = current.where(now, scope, subscription=True)
    # Подписка с поиска объявлений присылает объявление целиком — со всеми лотами.
    source = (
        "lots l, announcements a" if scope == "lots"
        else "announcements a JOIN lots l ON l.trd_buy_id = a.id"
    )
    return conn.execute(
        f"""INSERT INTO matches (subscription_id, lot_id, notified_at)
            SELECT %(sub_id)s, l.id, {"now()" if notified else "NULL"} FROM {source} WHERE {where}
            ON CONFLICT DO NOTHING""",
        {**params, "sub_id": sub_id},
    ).rowcount


# Новая (или изменённая) подписка присылает всё, что подходит сейчас, одним сообщением
# с листанием: по PAGE_SIZE объявлений на странице, кнопки «Назад» / «Дальше».
# Не больше PAGE_LOTS_LIMIT лотов с ближайшим сроком: у широкой подписки их тысячи, а
# каждая страница собирается заново из всех лотов выборки.
PAGE_SIZE = 10
PAGE_LOTS_LIMIT = 200


def seed(conn: psycopg.Connection, sub_id: int, current: Filters, scope: str,
         now: datetime | None = None, send_open: bool = False) -> None:
    """Подходящее сейчас — сразу «увиденным»: подписка присылает только новое. send_open —
    по просьбе при создании ещё и одним сообщением всё, что уже открыто (с листанием)."""
    now = now or datetime.now(UTC)
    where, params = current.where(now, scope, subscription=True)
    source = ("lots l, announcements a" if scope == "lots"
              else "announcements a JOIN lots l ON l.trd_buy_id = a.id")
    rows = conn.execute(
        f"""INSERT INTO matches (subscription_id, lot_id, notified_at)
            SELECT %(sub_id)s, l.id, now() FROM {source} WHERE {where}
            ON CONFLICT DO NOTHING RETURNING lot_id""",
        {**params, "sub_id": sub_id},
    ).fetchall()
    if rows and send_open:
        ordered = conn.execute(
            """SELECT l.id, l.trd_buy_id FROM lots l LEFT JOIN announcements a ON a.id = l.trd_buy_id
               WHERE l.id = ANY(%s)
               ORDER BY a.end_date NULLS LAST, l.trd_buy_id, l.prepayment DESC NULLS LAST, l.id""",
            ([r[0] for r in rows],)).fetchall()
        # Объявление целиком: последнее не обрезаем на середине.
        nearest = []
        for lot_id, announcement in ordered:
            if len(nearest) >= PAGE_LOTS_LIMIT and announcement != ordered[len(nearest) - 1][1]:
                break
            nearest.append(lot_id)
        conn.execute("INSERT INTO match_pages (subscription_id, lot_ids, total) VALUES (%s, %s, %s)",
                     (sub_id, nearest, len(rows)))


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
    lot_name_kz: str | None = None
    announcement_name_kz: str | None = None
    price: Decimal | None = None
    disable_person: bool | None = None
    langs: str = "both"  # ru | kk | both


# Язык уведомлений: у группы — свой, в личку — выбранный в настройках, иначе язык сайта.
_LANGS_SQL = """CASE WHEN s.chat_id IS NOT NULL THEN coalesce(g.notify_lang, 'both')
                     ELSE coalesce(u.notify_lang, u.ui_lang, 'ru') END"""
_LOT_COLUMNS = """s.id,
                  -- В общей группе видно, чья это подписка.
                  CASE WHEN s.chat_id IS NULL THEN s.name ELSE s.name || ' · ' || u.email END,
                  coalesce(s.chat_id, u.telegram_chat_id), l.id, l.name_ru,
                  coalesce(l.extra_desc, l.description_ru), l.amount, l.count, l.unit_name,
                  l.prepayment, l.kato, l.trd_buy_id, coalesce(a.number_anno, l.number_anno),
                  a.name_ru, a.end_date,
                  coalesce(a.customer_name_ru, l.customer_name_ru, a.org_name_ru), a.kato,
                  l.name_kz, a.name_kz, l.price, a.disable_person, """ + _LANGS_SQL
LANGS = ("ru", "kk", "both")


def pending(conn: psycopg.Connection, limit: int = 2000) -> list[PendingLot]:
    # Тариф закончился или аккаунт отключён — совпадения не копятся до продления, а
    # пропускаются: иначе после оплаты пришла бы пачка старых лотов.
    conn.execute(
        """UPDATE matches m SET notified_at = now()
           FROM subscriptions s JOIN users u ON u.id = s.user_id
           WHERE m.subscription_id = s.id AND m.notified_at IS NULL AND NOT (""" + ENTITLED_SQL + ")"
    )
    rows = conn.execute(
        f"""SELECT {_LOT_COLUMNS}
           FROM matches m
           JOIN subscriptions s ON s.id = m.subscription_id
           JOIN users u ON u.id = s.user_id
           JOIN lots l ON l.id = m.lot_id
           LEFT JOIN announcements a ON a.id = l.trd_buy_id
           LEFT JOIN telegram_groups g ON g.chat_id = s.chat_id
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


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _money_line(lot: PendingLot, advance: str) -> str:
    parts = []
    if lot.price is not None and lot.count is not None:
        parts.append(f"{_money(lot.price)} × {_quantity(lot.count)}"
                     + (f" = {_money(lot.amount)}" if lot.amount is not None else ""))
    elif lot.amount is not None:
        parts.append(_money(lot.amount))
    if lot.prepayment:
        parts.append(f"<b>{advance} {float(lot.prepayment):g}%</b>")
    return "   " + " · ".join(parts) if parts else ""


def _part(lots: list[PendingLot], kazakh: bool) -> list[str]:
    """Одна языковая часть блока: название, номер и срок, признак, лоты."""
    first = lots[0]
    title = (first.announcement_name_kz if kazakh else None) or first.announcement_name \
        or first.lot_name or "—"
    lines = [f"<b>{html.escape(_cut(title, 160))}</b>"]
    head = [f"№ {html.escape(first.number_anno or str(first.announcement_id or ''))}"]
    if first.end_date:
        head.append(f"⏰ {first.end_date.astimezone(PORTAL_TZ):%d.%m %H:%M}")
    lines.append(" · ".join(head))
    if first.disable_person:
        lines.append("♿ " + ("Мүгедектер ұйымдары арасында" if kazakh
                             else "Среди организаций инвалидов"))
    for lot in lots[:LOTS_PER_BLOCK]:
        name = (lot.lot_name_kz if kazakh else None) or lot.lot_name or "—"
        lines.append(f"• <b>{html.escape(_cut(name, 120))}</b>")
        if money := _money_line(lot, "аванс"):
            lines.append(money)
    if len(lots) > LOTS_PER_BLOCK:
        rest = len(lots) - LOTS_PER_BLOCK
        lines.append(f"…тағы {rest} лот" if kazakh else f"…и ещё {rest} лот.")
    return lines


def format_block(lots: list[PendingLot]) -> str:
    """Блок одного объявления на языке уведомлений; оба языка — сначала целиком
    по-казахски, потом целиком по-русски. В конце — ссылка на наш сайт."""
    langs = lots[0].langs
    if langs == "ru":
        lines = _part(lots, kazakh=False)
    elif langs == "kk":
        lines = _part(lots, kazakh=True)
    else:
        lines = ["🇰🇿", *_part(lots, kazakh=True), "", "🇷🇺", *_part(lots, kazakh=False)]
    if lots[0].announcement_id:
        lines += ["", announcement_url(lots[0].announcement_id)]
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


def _already_sent(conn: psycopg.Connection, found: list[PendingLot]) -> set[tuple[str, int]]:
    pairs = {(lot.chat_id, lot.lot_id) for lot in found if lot.chat_id}
    if not pairs:
        return set()
    rows = conn.execute(
        """SELECT chat_id, lot_id FROM chat_lots
           WHERE (chat_id, lot_id) IN (SELECT * FROM unnest(%s::text[], %s::bigint[]))""",
        ([p[0] for p in pairs], [p[1] for p in pairs]),
    )
    return {(row[0], row[1]) for row in rows}


def _remember_sent(conn: psycopg.Connection, chat_id: str, batch: list[Notice]) -> None:
    lots = sorted({lot_id for notice in batch for _, lot_id in notice.keys})
    conn.execute(
        """INSERT INTO chat_lots (chat_id, lot_id) SELECT %s, unnest(%s::bigint[])
           ON CONFLICT DO NOTHING""",
        (chat_id, lots),
    )


def _without_repeats(conn: psycopg.Connection, found_lots: list) -> list:
    """Убирает лоты, которые уже были в этом чате или пришли по второй подписке в тот же
    чат; такие совпадения сразу помечаются обработанными."""
    sent_before = _already_sent(conn, found_lots)
    fresh, repeated = [], []
    seen: set[tuple[str | None, int]] = set()
    for lot in found_lots:
        key = (lot.chat_id, lot.lot_id)
        if lot.chat_id and (key in sent_before or key in seen):
            repeated.append((lot.subscription_id, lot.lot_id))
            continue
        seen.add(key)
        fresh.append(lot)
    _mark_notified(conn, [Notice(None, "", "", repeated)])
    return fresh


def _chat_gone(conn: psycopg.Connection, chat_id: str, chat_notices: list) -> None:
    """Чат удалён или бот выгнан: отвязываем. Подписки группы теперь шлют в личку —
    эти лоты уйдут туда; личные — считаются обработанными."""
    logger.info("chat %s is gone, unlinking", chat_id)
    conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s", (chat_id,))
    is_group = conn.execute("SELECT 1 FROM telegram_groups WHERE chat_id = %s", (chat_id,)).fetchone()
    bot.unlink_group(conn, chat_id)
    if is_group:
        conn.commit()
    else:
        _mark_notified(conn, chat_notices)


def deliver(conn: psycopg.Connection, pause: float = 1.1) -> int:
    """Рассылает новые совпадения. Возвращает число отправленных лотов.

    Лот приходит в чат один раз, даже если подошёл под несколько подписок.
    """
    send_pages(conn)
    found = notices(_without_repeats(conn, pending(conn)))
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
                if error.code == 400 and not error.chat_gone:
                    # Сообщение не принято (слишком длинное, кривая разметка): иначе
                    # оно повторялось бы каждый раз и держало всю очередь.
                    logger.warning("message to %s rejected, skipped: %s", chat_id, error)
                    _mark_notified(conn, batch)
                    continue
                if not error.chat_gone:
                    # Telegram недоступен или лимит: остальное отправим в следующий раз.
                    logger.warning("delivery stopped: %s", error)
                    return sent
                _chat_gone(conn, chat_id, chat_notices)
                break
            _remember_sent(conn, chat_id, batch)
            _mark_notified(conn, batch)
            sent += sum(len(n.keys) for n in batch)
            # Telegram разрешает в один чат около сообщения в секунду.
            time.sleep(pause)
    return sent


# --- Первая выборка подписки: одно сообщение с листанием -----------------------------

ENTITLED_PAGES = "SELECT 1 FROM users u WHERE u.id = s.user_id AND " + ENTITLED_SQL


def _page_lots(conn: psycopg.Connection, sub_id: int, lot_ids: list[int]) -> list[PendingLot]:
    rows = conn.execute(
        f"""SELECT {_LOT_COLUMNS}
           FROM subscriptions s
           JOIN users u ON u.id = s.user_id
           JOIN lots l ON l.id = ANY(%(ids)s)
           LEFT JOIN announcements a ON a.id = l.trd_buy_id
           LEFT JOIN telegram_groups g ON g.chat_id = s.chat_id
           WHERE s.id = %(sub)s
           ORDER BY a.end_date NULLS LAST, l.trd_buy_id, l.prepayment DESC NULLS LAST, l.id""",
        {"ids": lot_ids, "sub": sub_id},
    ).fetchall()
    return [PendingLot(*row) for row in rows]


def _pages(blocks: list[Notice]) -> list[list[Notice]]:
    """По PAGE_SIZE объявлений, но не длиннее одного сообщения Telegram."""
    pages: list[list[Notice]] = []
    page: list[Notice] = []
    size = 0
    for block in blocks:
        if page and (len(page) >= PAGE_SIZE or size + len(block.text) + 2 > telegram.MESSAGE_LIMIT - 300):
            pages.append(page)
            page, size = [], 0
        page.append(block)
        size += len(block.text) + 2
    if page:
        pages.append(page)
    return pages


_NAV = {"ru": ("◀ Назад", "Дальше ▶"), "kk": ("◀ Артқа", "Әрі қарай ▶"),
        "both": ("◀ Артқа · Назад", "Әрі қарай · Дальше ▶")}
# Первое сообщение подписки — не новые лоты, а то, что уже открыто: так и пишем.
_FOUND = {"ru": "Уже открытые закупки (опубликованы раньше): {n} объявл. · стр. {p} из {t}",
          "kk": "Қазір ашық сатып алулар (бұрын жарияланған): {n} хабарландыру · {p}/{t} бет",
          "both": "Қазір ашық / уже открытые (бұрын жарияланған / опубликованы раньше): {n} · {p}/{t}"}
_FIRST = {"ru": "Уже открытые закупки (опубликованы раньше). Новые будут приходить по мере публикации.",
          "kk": "Қазір ашық сатып алулар (бұрын жарияланған). Жаңалары жарияланған сайын келеді.",
          "both": "Қазір ашық (бұрын жарияланған) / уже открытые (опубликованы раньше). "
                  "Жаңалары / новые — по мере публикации."}
_PARTIAL = {"ru": "Показаны {shown} лотов с ближайшим сроком из {total}, остальные — в поиске на сайте.",
            "kk": "Мерзімі ең жақын {shown} лот көрсетілді ({total} ішінен), қалғаны — сайттағы іздеуде.",
            "both": "Көрсетілді / показаны: {shown} из {total} · мерзімі жақын / ближайшие по сроку"}


def render_page(conn: psycopg.Connection, page_id: int, number: int) -> tuple[str, list | None] | None:
    """Текст и кнопки страницы number (с нуля) первой выборки; None — выборки нет."""
    row = conn.execute("SELECT subscription_id, lot_ids, total FROM match_pages WHERE id = %s",
                       (page_id,)).fetchone()
    if row is None:
        return None
    lots = _page_lots(conn, row[0], row[1])
    if not lots:
        return None
    blocks = notices(lots)
    pages = _pages(blocks)
    number = min(max(number, 0), len(pages) - 1)
    langs = lots[0].langs
    head = f"🔔 <b>{html.escape(lots[0].subscription_name)}</b>"
    if len(pages) > 1:
        head += "\n" + _FOUND[langs].format(n=len(blocks), p=number + 1, t=len(pages))
    else:
        head += "\n" + _FIRST[langs]
    # Показана часть — только если выборку обрезал предел (а не убраны уже приходившие в чат).
    if row[2] and row[2] > PAGE_LOTS_LIMIT and row[2] > len(row[1]):
        head += "\n" + _PARTIAL[langs].format(shown=len(row[1]), total=row[2])
    text = head + "\n\n" + "\n\n".join(block.text for block in pages[number])
    if len(pages) == 1:
        return text, None
    back, forward = _NAV[langs]
    buttons = []
    if number > 0:
        buttons.append({"text": back, "callback_data": f"mp:{page_id}:{number - 1}"})
    if number < len(pages) - 1:
        buttons.append({"text": forward, "callback_data": f"mp:{page_id}:{number + 1}"})
    return text, [buttons]


def send_pages(conn: psycopg.Connection) -> int:
    """Отправляет первые страницы новых выборок. Возвращает число отправленных."""
    sent = 0
    for page_id, sub_id, lot_ids in conn.execute(
        """SELECT p.id, p.subscription_id, p.lot_ids FROM match_pages p
           JOIN subscriptions s ON s.id = p.subscription_id
           WHERE p.sent_at IS NULL AND EXISTS (""" + ENTITLED_PAGES + """) ORDER BY p.id"""
    ).fetchall():
        chat = conn.execute(
            """SELECT coalesce(s.chat_id, u.telegram_chat_id) FROM subscriptions s
               JOIN users u ON u.id = s.user_id WHERE s.id = %s""", (sub_id,)).fetchone()[0]
        # Уже приходившие в этот чат лоты (по другой подписке) повторно не показываем.
        sent_before = _already_sent(conn, [_Key(chat, i) for i in lot_ids]) if chat else set()
        fresh = [i for i in lot_ids if (chat, i) not in sent_before] if chat else []
        if fresh != lot_ids:
            conn.execute("UPDATE match_pages SET lot_ids = %s WHERE id = %s", (fresh, page_id))
        page = render_page(conn, page_id, 0) if fresh else None
        if page is None:
            conn.execute("UPDATE match_pages SET sent_at = now() WHERE id = %s", (page_id,))
            conn.commit()
            continue
        conn.commit()
        try:
            message = telegram.send_message(chat, page[0], buttons=page[1])
        except telegram.TelegramError as error:
            if not error.chat_gone and error.code != 400:
                logger.warning("pages stopped: %s", error)
                return sent
            logger.warning("page %s to %s not sent: %s", page_id, chat, error)
            message = None
        conn.execute("UPDATE match_pages SET sent_at = now(), chat_id = %s, message_id = %s WHERE id = %s",
                     (chat, (message or {}).get("message_id"), page_id))
        if message:
            conn.execute("INSERT INTO chat_lots (chat_id, lot_id) SELECT %s, unnest(%s::bigint[]) "
                         "ON CONFLICT DO NOTHING", (chat, fresh))
            sent += 1
        conn.commit()
    return sent


class _Key(NamedTuple):
    chat_id: str
    lot_id: int


def turn_page(conn: psycopg.Connection, query: dict, bot_name: str = "main") -> None:
    """Кнопка «Назад» / «Дальше» под первой выборкой: показать другую страницу."""
    try:
        _, page_id, number = (query.get("data") or "").split(":")
        page_id, number = int(page_id), int(number)
        if not 0 < page_id < 2**63:
            raise ValueError(page_id)
    except ValueError:  # испорченная кнопка — не повод падать боту
        page_id = number = None
    message = query.get("message") or {}
    chat = str((message.get("chat") or {}).get("id"))
    if bot_name == "client":
        chat = telegram.CLIENT_PREFIX + chat
    owner = conn.execute("SELECT chat_id FROM match_pages WHERE id = %s",
                         (page_id,)).fetchone() if page_id is not None else None
    page = render_page(conn, page_id, number) if owner and owner[0] == chat else None
    conn.commit()
    try:
        telegram.call("answerCallbackQuery", bot=bot_name, callback_query_id=query["id"],
                      **({} if page else {"text": "Список устарел"}))
        if page:
            telegram.call("editMessageText", bot=bot_name, chat_id=message["chat"]["id"],
                          message_id=message["message_id"], text=page[0], parse_mode="HTML",
                          link_preview_options={"is_disabled": True},
                          reply_markup={"inline_keyboard": page[1] or []})
    except telegram.TelegramError as error:
        logger.warning("page turn failed: %s", error)
