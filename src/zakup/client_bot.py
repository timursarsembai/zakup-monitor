"""Клиентский Telegram-бот (@tenderim_kz_bot): уведомления клиентам и группа клиентов.

- Привязка личного чата: на сайте «Подключить Telegram» → t.me/<бот>?start=<код>.
  Чат хранится как users.telegram_chat_id = "c:<id>" — рассылки сами идут этим ботом.
- Группа клиентов: вступление только по заявке. Бот одобряет заявку, если у человека
  оплачен тариф и Telegram привязан к аккаунту, иначе отклоняет и пишет, что сделать.
- Раз в полчаса: за 3 дня до конца тарифа — напоминание, после конца — удаление
  из группы. Группа запоминается, когда администратор группы пишет в ней /start.
"""

import logging
import os
import time
from datetime import datetime, timedelta, UTC

import psycopg

from zakup import SITE_URL, bot, db, telegram

logger = logging.getLogger(__name__)

BOT = "client"
OFFSET_KEY = "telegram:client-offset"
MAINTENANCE_EVERY = timedelta(minutes=30)
WARN_BEFORE = timedelta(days=3)

HELP = (
    "Это бот Tenderim — мониторинга госзакупок Казахстана.\n\n"
    f"Чтобы получать уведомления, откройте {SITE_URL}/settings и нажмите «Подключить Telegram».\n"
    "/stop — отключить уведомления."
)


def group_id(conn: psycopg.Connection) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'client_group'").fetchone()
    return row[0] if row else None


def _set_group(conn: psycopg.Connection, chat_id: str, title: str) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES ('client_group', %s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (chat_id,),
    )
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES ('client_group_title', %s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (title[:200],),
    )
    conn.commit()


def _group_call(conn: psycopg.Connection, method: str, **params):
    """Запрос к группе клиентов. Если группа стала супергруппой (служебное сообщение
    об этом бот может не получить), берём новый id из ответа Telegram и повторяем."""
    group = group_id(conn)
    try:
        return telegram.call(method, bot=BOT, chat_id=group, **params)
    except telegram.TelegramError as error:
        new_id = error.parameters.get("migrate_to_chat_id")
        if not new_id:
            raise
        title = conn.execute("SELECT value FROM app_settings WHERE key = 'client_group_title'").fetchone()
        _set_group(conn, str(new_id), title[0] if title else "")
        return telegram.call(method, bot=BOT, chat_id=str(new_id), **params)


def _send(chat_id: int | str, text: str) -> None:
    try:
        telegram.call("sendMessage", bot=BOT, chat_id=chat_id, text=text,
                      link_preview_options={"is_disabled": True})
    except telegram.TelegramError as error:
        logger.info("client bot: message to %s not sent: %s", chat_id, error)


def _user_by_tg(conn: psycopg.Connection, tg_id: int) -> tuple | None:
    """(id, логин, есть ли доступ) по Telegram-аккаунту, привязанному к клиентскому боту."""
    return conn.execute(
        """SELECT id, email, role IN ('admin', 'moderator') OR coalesce(paid_until > now(), false)
           FROM users WHERE telegram_chat_id = %s AND deleted_at IS NULL AND NOT disabled""",
        (f"{telegram.CLIENT_PREFIX}{tg_id}",),
    ).fetchone()


def _site_admin(conn: psycopg.Connection, tg_id: int | None) -> bool:
    if not tg_id:
        return False
    if os.environ.get("ZAKUP_TELEGRAM_CHAT_ID", "").strip() == str(tg_id):
        return True
    return conn.execute(
        """SELECT 1 FROM users WHERE role = 'admin' AND deleted_at IS NULL AND NOT disabled
           AND telegram_chat_id = ANY(%s)""",
        ([str(tg_id), f"{telegram.CLIENT_PREFIX}{tg_id}"],),
    ).fetchone() is not None


def handle_message(conn: psycopg.Connection, message: dict) -> str | None:
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    command, _, argument = text.partition(" ")
    command = command.partition("@")[0]
    if chat.get("type") in ("group", "supergroup"):
        # Группа стала супергруппой (нужно для заявок) — у неё новый id.
        if (new_id := message.get("migrate_to_chat_id")) and str(chat["id"]) == group_id(conn):
            _set_group(conn, str(new_id), chat.get("title") or "")
            return None
        if command != "/start":
            return None
        sender = (message.get("from") or {}).get("id")
        try:
            member = telegram.call("getChatMember", bot=BOT, chat_id=chat["id"], user_id=sender)
        except telegram.TelegramError:
            return None
        # Подключить группу может только администратор сайта, а не админ любой группы:
        # иначе чужая группа подменила бы нашу и получала бы ссылки для клиентов.
        if member.get("status") not in ("creator", "administrator") or not _site_admin(conn, sender):
            return None
        _set_group(conn, str(chat["id"]), chat.get("title") or "")
        return ("Группа клиентов подключена. Вступление — по заявкам: бот одобряет тех, "
                "у кого оплачен тариф, и удаляет тех, у кого он закончился.")
    if chat.get("type") != "private":
        return None
    chat_id = f"{telegram.CLIENT_PREFIX}{chat['id']}"
    if command == "/start" and argument:
        user_id = bot._take_code(conn, argument, "client")
        if user_id is None:
            conn.commit()
            return f"Ссылка устарела или уже использована. Возьмите новую на сайте: {SITE_URL}/settings"
        conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s AND id <> %s",
                     (chat_id, user_id))
        login = conn.execute("UPDATE users SET telegram_chat_id = %s WHERE id = %s RETURNING email",
                             (chat_id, user_id)).fetchone()[0]
        conn.commit()
        return (f"Готово, уведомления для {login} будут приходить сюда.\n"
                "Подписки настраиваются на сайте. /stop — отключить.")
    if command == "/stop":
        gone = conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s",
                            (chat_id,)).rowcount
        conn.commit()
        return "Уведомления отключены." if gone else "Этот чат и так не подключён."
    return HELP


def handle_join_request(conn: psycopg.Connection, request: dict) -> str:
    """Заявка в группу клиентов: одобряем оплативших, остальным — отказ и подсказка."""
    chat_id = (request.get("chat") or {}).get("id")
    tg_id = (request.get("from") or {}).get("id")
    if not chat_id or not tg_id or str(chat_id) != (group_id(conn) or ""):
        return "not our group"
    user = _user_by_tg(conn, tg_id)
    dm = request.get("user_chat_id") or tg_id
    if user and user[2]:
        old = conn.execute("SELECT tg_id FROM client_group_members WHERE user_id = %s",
                           (user[0],)).fetchone()
        conn.commit()
        telegram.call("approveChatJoinRequest", bot=BOT, chat_id=chat_id, user_id=tg_id)
        if old and old[0] != tg_id:
            # Один оплаченный аккаунт — один человек в группе: прежний Telegram удаляем.
            _remove(conn, old[0])
        conn.execute(
            """INSERT INTO client_group_members (user_id, tg_id) VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE SET tg_id = excluded.tg_id, joined_at = now(),
                   warned_at = NULL""",
            (user[0], tg_id),
        )
        conn.commit()
        return "approved"
    telegram.call("declineChatJoinRequest", bot=BOT, chat_id=chat_id, user_id=tg_id)
    if user is None:
        _send(dm, "Чтобы вступить в группу клиентов, привяжите этот Telegram к аккаунту на сайте: "
                  f"{SITE_URL}/settings → «Подключить Telegram», затем подайте заявку снова.")
    else:
        _send(dm, f"Группа — для клиентов с оплаченным тарифом. Оплатить: {SITE_URL}/pricing — "
                  "после оплаты подайте заявку снова.")
    return "declined"


def maintenance(conn: psycopg.Connection, now: datetime | None = None) -> tuple[int, int]:
    """Напоминания за 3 дня и удаление из группы тех, у кого тариф закончился."""
    now = now or datetime.now(UTC)
    group = group_id(conn)
    if not group:
        return 0, 0
    warned = 0
    soon = conn.execute(
        """SELECT m.user_id, m.tg_id, u.paid_until FROM client_group_members m
           JOIN users u ON u.id = m.user_id
           WHERE u.role = 'user' AND u.paid_until > %s AND u.paid_until <= %s
             AND (m.warned_at IS NULL OR m.warned_at < u.paid_until - %s)""",
        (now, now + WARN_BEFORE, WARN_BEFORE),
    ).fetchall()
    conn.commit()  # рассылка — без открытой транзакции
    for user_id, tg_id, until in soon:
        _send(tg_id, f"Тариф Tenderim заканчивается {until.strftime('%d.%m.%Y')}. После этого бот "
                     f"удалит вас из группы клиентов. Продлить: {SITE_URL}/pricing")
        conn.execute("UPDATE client_group_members SET warned_at = %s WHERE user_id = %s", (now, user_id))
        warned += 1
    conn.commit()
    removed = 0
    expired = conn.execute(
        """SELECT m.user_id, m.tg_id FROM client_group_members m JOIN users u ON u.id = m.user_id
           WHERE u.role = 'user' AND (u.paid_until IS NULL OR u.paid_until <= %s
                                      OR u.deleted_at IS NOT NULL OR u.disabled)""",
        (now,),
    ).fetchall()
    conn.commit()
    for user_id, tg_id in expired:
        if not _remove(conn, tg_id):
            continue
        conn.execute("DELETE FROM client_group_members WHERE user_id = %s", (user_id,))
        conn.commit()
        _send(tg_id, f"Тариф закончился, поэтому бот удалил вас из группы клиентов. Продлить: "
                     f"{SITE_URL}/pricing — затем снова подайте заявку в группу.")
        removed += 1
    return warned, removed


def _remove(conn: psycopg.Connection, tg_id: int) -> bool:
    """Удалить из группы без вечного бана: бан и сразу снятие — вернуться можно по заявке."""
    try:
        _group_call(conn, "banChatMember", user_id=tg_id)
        _group_call(conn, "unbanChatMember", user_id=tg_id, only_if_banned=True)
    except telegram.TelegramError as error:
        logger.warning("client bot: remove %s failed: %s", tg_id, error)
        return False
    return True


def invite_link(conn: psycopg.Connection, login: str) -> str | None:
    """Ссылка-заявка в группу клиентов для человека с оплаченным тарифом."""
    if not group_id(conn):
        return None
    link = _group_call(conn, "createChatInviteLink", creates_join_request=True, name=login[:32])
    return link.get("invite_link")


def run(database_url: str, poll_timeout: int = 50) -> None:
    while not telegram.token(BOT):
        logger.warning("client bot: ZAKUP_CLIENT_BOT_TOKEN is not set, sleeping")
        time.sleep(3600)
    while True:
        try:
            telegram.call("deleteWebhook", bot=BOT)
            break
        except telegram.TelegramError as error:
            logger.warning("client bot: %s", error)
            time.sleep(30)
    logger.info("client bot @%s is polling", telegram.bot_username(BOT))
    next_maintenance = datetime.now(UTC)
    while True:
        try:
            with db.connect(database_url) as conn:
                if datetime.now(UTC) >= next_maintenance:
                    print(f"client group: warned/removed {maintenance(conn)}", flush=True)
                    next_maintenance = datetime.now(UTC) + MAINTENANCE_EVERY
                state = db.get_state(conn, OFFSET_KEY)
                conn.commit()  # ожидание Telegram — без открытой транзакции
                updates = telegram.call(
                    "getUpdates", bot=BOT, http_timeout=poll_timeout + 10,
                    offset=state.last_id if state else 0, timeout=poll_timeout,
                    allowed_updates=["message", "chat_join_request"],
                )
                for update in updates:
                    # Ошибка на одном обновлении (заявку уже разобрал человек, отменили) не
                    # должна стопорить все следующие: пропускаем его и идём дальше.
                    try:
                        if message := update.get("message"):
                            if reply := handle_message(conn, message):
                                _send(message["chat"]["id"], reply)
                        elif request := update.get("chat_join_request"):
                            logger.info("join request: %s", handle_join_request(conn, request))
                    except telegram.TelegramError as error:
                        conn.rollback()
                        logger.warning("client bot: update %s skipped: %s", update.get("update_id"), error)
                    db.save_state(conn, OFFSET_KEY, db.SyncState(update["update_id"] + 1, None, None))
                    conn.commit()
        except telegram.TelegramError as error:
            logger.warning("client bot: %s", error)
            time.sleep(10)
        except psycopg.Error as error:
            logger.warning("client bot: database %s", type(error).__name__)
            time.sleep(10)
