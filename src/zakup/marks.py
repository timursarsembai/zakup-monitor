"""Избранные лоты («♥ В избранное») — «Мои лоты».

Пока приём заявок открыт, бот напоминает об избранных лотах в Telegram — когда
выбрал пользователь (за 2 рабочих дня, за сутки, за 3 часа; по умолчанию за сутки и
за 3 часа); после итогов — кто выиграл (можно отключить). Язык — как у уведомлений
по подпискам.
"""

import html
import logging
import time
from datetime import UTC, datetime, timedelta

import psycopg

from zakup import telegram
from zakup.auth import ENTITLED_SQL
from zakup.goszakup import PORTAL_TZ
from zakup.matcher import lot_url

logger = logging.getLogger(__name__)

# Отметка одна — «в избранное» (status 'interested'); пустой статус — не отмечен.
# Колонка status оставлена, чтобы не трогать схему и старые записи.
STATUSES = {"interested": "В избранном"}
REMIND_STATUSES = ["interested"]
# Варианты напоминаний: «wd2» — за 2 рабочих дня, числа — за столько часов.
REMIND_OPTIONS = ("wd2", "24", "3")
REMIND_DEFAULT = ["24", "3"]
# С какого запаса до окончания вообще смотреть лоты (2 рабочих дня через выходные — до 4 суток).
REMIND_HORIZON = timedelta(days=5)
# Язык: выбранный для уведомлений, иначе язык сайта.
_LANGS_SQL = "coalesce(u.notify_lang, u.ui_lang, 'ru')"


def _say(langs: str, ru: str, kk: str) -> str:
    return ru if langs == "ru" else kk if langs == "kk" else f"{ru} / {kk}"


def _hours_before(end: datetime, option: str) -> int:
    """За сколько часов до окончания срабатывает вариант напоминания."""
    if option != "wd2":
        return int(option)
    moment, left = end, 2
    while left:
        moment -= timedelta(days=1)
        if moment.astimezone(PORTAL_TZ).weekday() < 5:
            left -= 1
    return int((end - moment).total_seconds() // 3600)
NOTE_LIMIT = 2000


def set_mark(conn: psycopg.Connection, user_id: int, lot_id: int, status: str) -> None:
    """Ставит статус; пустой статус без заметки снимает отметку."""
    if not status:
        conn.execute(
            "DELETE FROM lot_marks WHERE user_id = %s AND lot_id = %s AND note = ''",
            (user_id, lot_id),
        )
        conn.execute(
            "UPDATE lot_marks SET status = '', status_at = now(), updated_at = now() "
            "WHERE user_id = %s AND lot_id = %s",
            (user_id, lot_id),
        )
        return
    if status not in STATUSES:
        raise ValueError(status)
    conn.execute(
        """INSERT INTO lot_marks (user_id, lot_id, status) VALUES (%s, %s, %s)
           ON CONFLICT (user_id, lot_id) DO UPDATE SET
               status = EXCLUDED.status, updated_at = now(),
               status_at = CASE WHEN lot_marks.status = EXCLUDED.status
                                THEN lot_marks.status_at ELSE now() END,
               reminded_hours = CASE WHEN lot_marks.status = EXCLUDED.status
                                     THEN lot_marks.reminded_hours END""",
        (user_id, lot_id, status),
    )


def set_note(conn: psycopg.Connection, user_id: int, lot_id: int, note: str) -> None:
    note = note.strip()[:NOTE_LIMIT]
    conn.execute(
        """INSERT INTO lot_marks (user_id, lot_id, status, note) VALUES (%s, %s, '', %s)
           ON CONFLICT (user_id, lot_id) DO UPDATE SET note = EXCLUDED.note, updated_at = now()""",
        (user_id, lot_id, note),
    )
    conn.execute(
        "DELETE FROM lot_marks WHERE user_id = %s AND lot_id = %s AND status = '' AND note = ''",
        (user_id, lot_id),
    )


def of_user(conn: psycopg.Connection, user_id: int, lot_ids: list[int]) -> dict[int, tuple]:
    """{lot_id: (status, note)} для показа на страницах."""
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT lot_id, status, note FROM lot_marks WHERE user_id = %s AND lot_id = ANY(%s)",
            (user_id, lot_ids),
        )
    }


def _threshold(end: datetime, status_at: datetime, reminded: int | None, now: datetime,
               options: list[str] | None = None) -> int | None:
    """Какое напоминание пора отправить: самое близкое к сроку из наступивших.

    Только если отметку поставили раньше этого момента: иначе вы только что
    сами смотрели лот, и напоминание было бы лишним.
    """
    due = None
    for hours in sorted((_hours_before(end, o) for o in options or REMIND_DEFAULT), reverse=True):
        moment = end - timedelta(hours=hours)
        if moment <= now < end and status_at < moment and (reminded is None or hours < reminded):
            due = hours
    return due


def _left(end: datetime, now: datetime, langs: str = "ru") -> str:
    """Сколько осталось: «2 д 5 ч», «5 ч 10 мин», «40 мин»."""
    day, hour, minute = ("күн", "сағ", "мин") if langs == "kk" else ("д", "ч", "мин")
    minutes = int((end - now).total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} {day} {hours} {hour}"
    return f"{hours} {hour} {minutes} {minute}" if hours else f"{minutes} {minute}"


def remind(conn: psycopg.Connection, now: datetime | None = None, pause: float = 1.1) -> int:
    """Напоминает о сроке по отмеченным лотам. Возвращает число отправленных."""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """SELECT m.user_id, m.lot_id, m.status, m.status_at, m.reminded_hours, m.note,
                  u.telegram_chat_id, a.id, a.end_date, l.name_ru, l.amount,
                  coalesce(a.customer_name_ru, l.customer_name_ru), u.remind_at, l.name_kz,
                  """ + _LANGS_SQL + """
           FROM lot_marks m
           JOIN users u ON u.id = m.user_id
           JOIN lots l ON l.id = m.lot_id
           JOIN announcements a ON a.id = l.trd_buy_id
           WHERE m.status = ANY(%s) AND a.end_date > %s AND a.end_date <= %s
             AND u.telegram_chat_id IS NOT NULL AND """ + ENTITLED_SQL + """
           ORDER BY a.end_date""",
        (REMIND_STATUSES, now, now + REMIND_HORIZON),
    ).fetchall()
    sent = 0
    for (user_id, lot_id, _status, status_at, reminded, note, chat_id, _a_id, end, name, amount,
         customer, options, name_kz, langs) in rows:
        if options == []:
            continue  # напоминания выключены
        hours = _threshold(end, status_at, reminded, now, options)
        if hours is None:
            continue
        title = name_kz if langs == "kk" and name_kz else name
        lines = [
            f"⏰ <b>{_say(langs, 'Осталось', 'Қалды')}: {_left(end, now, langs)}</b> · "
            f"{end.astimezone(PORTAL_TZ):%d.%m %H:%M}",
            f"{html.escape(title or '—')} · {_money(amount)}",
            html.escape(customer or ""),
            "♥ " + _say(langs, "В избранном", "Таңдаулыда"),
        ]
        if note:
            lines.append(f"{_say(langs, 'Заметка', 'Жазба')}: {html.escape(note[:300])}")
        lines.append(f'<a href="{lot_url(lot_id)}">{_say(langs, "Открыть лот", "Лотты ашу")}</a>')
        try:
            telegram.send_message(chat_id, "\n".join(line for line in lines if line))
        except telegram.TelegramError as error:
            logger.warning("reminder not sent: %s", error)
            if not error.chat_gone:
                break
            # Чата нет — не пытаемся снова каждые 10 минут.
            conn.execute(
                "UPDATE lot_marks SET reminded_hours = %s WHERE user_id = %s AND lot_id = %s",
                (hours, user_id, lot_id),
            )
            conn.commit()
            continue
        conn.execute(
            "UPDATE lot_marks SET reminded_hours = %s WHERE user_id = %s AND lot_id = %s",
            (hours, user_id, lot_id),
        )
        conn.commit()
        sent += 1
        time.sleep(pause)
    return sent


OUTCOME_TEXT = {"winner": ("Итоги подведены", "Қорытынды шығарылды"),
                "failed": ("Закупка не состоялась", "Сатып алу өтпеді")}


def announce_results(conn: psycopg.Connection, pause: float = 1.1) -> int:
    """Сообщает итог по избранным лотам; если в протоколе есть БИН организации
    пользователя — пишет, выиграли или нет."""
    rows = conn.execute(
        """SELECT m.user_id, m.lot_id, m.status, u.telegram_chat_id, u.company_bin,
                  r.outcome, r.reason, r.winner_bin, r.winner_name, r.winner_total, r.offers,
                  l.trd_buy_id, l.name_ru, l.amount, u.notify_results, l.name_kz, """ + _LANGS_SQL + """
           FROM lot_marks m
           JOIN users u ON u.id = m.user_id
           JOIN lot_results r ON r.lot_id = m.lot_id
           JOIN lots l ON l.id = m.lot_id
           WHERE m.status NOT IN ('', 'rejected') AND m.result_notified_at IS NULL
             AND """ + ENTITLED_SQL
    ).fetchall()
    sent = 0
    for (user_id, lot_id, _status, chat_id, company_bin, outcome, reason, winner_bin,
         winner_name, winner_total, offers, _a_id, name, amount, wanted, name_kz, langs) in rows:
        mine = company_bin and any(o.get("bin") == company_bin for o in offers or [])
        title = name_kz if langs == "kk" and name_kz else name
        lines = [f"🏁 <b>{_say(langs, *OUTCOME_TEXT[outcome]) if outcome in OUTCOME_TEXT else outcome}</b>",
                 f"{html.escape(title or '—')} · {_money(amount)}"]
        if outcome == "winner":
            lines.append(f"{_say(langs, 'Победитель', 'Жеңімпаз')}: {html.escape(winner_name or winner_bin or '—')}"
                         f"{' — ' + _money(winner_total) if winner_total is not None else ''}")
        elif reason:
            lines.append(f"{_say(langs, 'Причина', 'Себебі')}: {html.escape(reason)}")
        lines.append(f"{_say(langs, 'Предложений', 'Ұсыныстар')}: {len(offers or [])}")
        if mine:
            won = winner_bin == company_bin
            lines.append("🎉 " + _say(langs, "Вы выиграли", "Сіз ұттыңыз") if won
                         else _say(langs, "Вы не выиграли", "Сіз ұтпадыңыз"))
        lines.append(f'<a href="{lot_url(lot_id)}">{_say(langs, "Открыть лот", "Лотты ашу")}</a>')
        if chat_id and wanted:
            try:
                telegram.send_message(chat_id, "\n".join(lines))
            except telegram.TelegramError as error:
                logger.warning("result not sent: %s", error)
                if not error.chat_gone:
                    break
        conn.execute(
            "UPDATE lot_marks SET result_notified_at = now() WHERE user_id = %s AND lot_id = %s",
            (user_id, lot_id),
        )
        conn.commit()
        sent += 1
        if chat_id and wanted:
            time.sleep(pause)
    return sent


def _money(value) -> str:
    return f"{value:,.2f}".replace(",", " ").removesuffix(".00") + " ₸" if value is not None else ""
