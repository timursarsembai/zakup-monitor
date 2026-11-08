"""Telegram-бот: привязка чатов к аккаунту сайта. Работает long polling'ом.

Совпадения рассылает не он, а worker (matcher.deliver); бот только отвечает
на команды /start <код>, /start, /stop. Личный чат привязывается ссылкой
t.me/<бот>?start=<код>, группа — t.me/<бот>?startgroup=<код>: Telegram сам
предложит выбрать группу, добавит туда бота и отправит в группе /start <код>.
"""

import hashlib
import logging
import secrets
import time
from datetime import timedelta

import psycopg

from zakup import SITE_URL, config, db, telegram

logger = logging.getLogger(__name__)

LINK_TTL = timedelta(minutes=30)
OFFSET_KEY = "telegram:offset"


HELP = (
    "Это бот сервиса zakup — мониторинга госзакупок.\n\n"
    f"Чтобы получать уведомления, откройте {SITE_URL}/settings и нажмите "
    "«Подключить Telegram».\n/stop — отключить уведомления."
)


def _code_hash(code: str) -> bytes:
    return hashlib.sha256(code.encode()).digest()


def create_link_code(conn: psycopg.Connection, user_id: int, kind: str = "private") -> str:
    # Telegram пропускает в параметре start только [A-Za-z0-9_-] и до 64 символов.
    code = secrets.token_urlsafe(24)
    conn.execute(
        "DELETE FROM telegram_link_codes WHERE (user_id = %s AND kind = %s) OR expires_at < now()",
        (user_id, kind),
    )
    conn.execute(
        """INSERT INTO telegram_link_codes (code_hash, user_id, expires_at, kind)
           VALUES (%s, %s, now() + %s, %s)""",
        (_code_hash(code), user_id, LINK_TTL, kind),
    )
    conn.commit()
    return code


def link_url(code: str) -> str:
    return f"https://t.me/{telegram.bot_username()}?start={code}"


def group_link_url(code: str) -> str:
    return f"https://t.me/{telegram.bot_username()}?startgroup={code}"


def _take_code(conn: psycopg.Connection, code: str, kind: str) -> int | None:
    row = conn.execute(
        """DELETE FROM telegram_link_codes
           WHERE code_hash = %s AND expires_at > now() AND kind = %s RETURNING user_id""",
        (_code_hash(code.strip()), kind),
    ).fetchone()
    return row[0] if row else None


def unlink_group(conn: psycopg.Connection, chat_id: str) -> None:
    """Бота убрали из группы или её отключили: подписки этой группы идут в личку."""
    conn.execute("UPDATE subscriptions SET chat_id = NULL WHERE chat_id = %s", (chat_id,))
    conn.execute("DELETE FROM telegram_groups WHERE chat_id = %s", (chat_id,))


def _is_chat_admin(chat_id: str, user_id: int | None) -> bool:
    if user_id is None:
        return False
    try:
        member = telegram.call("getChatMember", chat_id=chat_id, user_id=user_id)
    except telegram.TelegramError:
        return False
    return member.get("status") in ("creator", "administrator")


def _handle_group(conn: psycopg.Connection, chat: dict, command: str, argument: str,
                  sender: int | None = None) -> str | None:
    chat_id = str(chat["id"])
    if command == "/start" and argument:
        user_id = _take_code(conn, argument, "group")
        if user_id is None:
            conn.commit()
            return f"Ссылка устарела или уже использована. Возьмите новую: {SITE_URL}/settings"
        conn.execute(
            """INSERT INTO telegram_groups (chat_id, title) VALUES (%s, %s)
               ON CONFLICT (chat_id) DO UPDATE SET title = excluded.title""",
            (chat_id, chat.get("title")),
        )
        conn.execute(
            "INSERT INTO user_groups (user_id, chat_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, chat_id),
        )
        email = conn.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()[0]
        conn.commit()
        return (
            f"Группа подключена для {email}. Чтобы присылать сюда результаты, выберите эту "
            f"группу в подписке на сайте: {SITE_URL}/subscriptions"
        )
    if command == "/stop":
        # Отключить группу может только её администратор, а не любой участник.
        if not _is_chat_admin(chat_id, sender):
            return "Отключить уведомления может только администратор группы."
        unlink_group(conn, chat_id)
        conn.commit()
        return "Группа отключена: уведомления сюда больше не придут."
    return None


def handle(conn: psycopg.Connection, message: dict) -> str | None:
    """Обрабатывает входящее сообщение, возвращает ответ (или None — молчать)."""
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    command, _, argument = text.partition(" ")
    command, _, addressee = command.partition("@")
    if chat.get("type") in ("group", "supergroup"):
        # Группа стала супергруппой — у неё новый id.
        if new_id := message.get("migrate_to_chat_id"):
            conn.execute("UPDATE telegram_groups SET chat_id = %s WHERE chat_id = %s",
                         (str(new_id), str(chat["id"])))
            conn.execute("UPDATE subscriptions SET chat_id = %s WHERE chat_id = %s",
                         (str(new_id), str(chat["id"])))
            conn.execute("UPDATE chat_lots SET chat_id = %s WHERE chat_id = %s",
                         (str(new_id), str(chat["id"])))
            conn.commit()
            return None
        if addressee and addressee.lower() != (telegram.bot_username() or "").lower():
            return None
        return _handle_group(conn, chat, command, argument, (message.get("from") or {}).get("id"))
    if chat.get("type") != "private":
        return None
    chat_id = str(chat["id"])

    if command == "/start" and argument:
        user_id = _take_code(conn, argument, "private")
        row = (user_id,) if user_id is not None else None
        if row is None:
            conn.commit()
            return (
                f"Ссылка устарела или уже использована. Возьмите новую на сайте: {SITE_URL}/settings"
            )
        # Один чат — один аккаунт: отвязываем его от прежнего, если был.
        conn.execute(
            "UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s AND id <> %s",
            (chat_id, row[0]),
        )
        email = conn.execute(
            "UPDATE users SET telegram_chat_id = %s WHERE id = %s RETURNING email",
            (chat_id, row[0]),
        ).fetchone()[0]
        conn.commit()
        return (
            f"Готово, уведомления для {email} будут приходить сюда.\n"
            "Подписки настраиваются на сайте. /stop — отключить."
        )

    if command == "/stop":
        unlinked = conn.execute(
            "UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s", (chat_id,)
        ).rowcount
        conn.commit()
        return "Уведомления отключены." if unlinked else "Этот чат и так не подключён."

    return HELP


def handle_callback(conn: psycopg.Connection, query: dict) -> str:
    """Кнопки под отчётом дежурного: «Выложить» / «Отклонить». Только владелец, в своём чате."""
    owner = config.owner_chat_id()
    chat = ((query.get("message") or {}).get("chat") or {}).get("id")
    sender = (query.get("from") or {}).get("id")
    if not owner or str(chat) != owner or str(sender) != owner:
        return "Это может только владелец."
    kind, _, rest = (query.get("data") or "").partition(":")
    action, _, incident = rest.partition(":")
    if kind != "autofix" or action not in ("approve", "reject") or not incident.isdigit():
        return "Неизвестная кнопка."
    status = "approved" if action == "approve" else "rejected"
    done = conn.execute("UPDATE incidents SET status = %s WHERE id = %s AND status = 'proposed'",
                        (status, int(incident))).rowcount
    conn.commit()
    if not done:
        return "Уже решено раньше."
    return (f"Сбой №{incident}: выкладываю — пришлю итог." if status == "approved"
            else f"Сбой №{incident}: правка отклонена.")


def _answer(query: dict, text: str) -> None:
    try:
        telegram.call("answerCallbackQuery", callback_query_id=query["id"], text=text)
        message = query.get("message") or {}
        if message.get("message_id"):
            telegram.call("editMessageReplyMarkup", chat_id=message["chat"]["id"],
                          message_id=message["message_id"])
            telegram.send_message(message["chat"]["id"], text)
    except telegram.TelegramError as error:
        logger.warning("callback answer not sent: %s", error)


def _update(conn: psycopg.Connection, update: dict) -> None:
    if query := update.get("callback_query"):
        if (query.get("data") or "").startswith("mp:"):
            from zakup import matcher  # matcher сам импортирует bot

            matcher.turn_page(conn, query)
        else:
            _answer(query, handle_callback(conn, query))
    message = update.get("message")
    if message and (reply := handle(conn, message)):
        try:
            telegram.send_message(message["chat"]["id"], reply)
        except telegram.TelegramError as error:
            logger.warning("reply not sent: %s", error)


def run(database_url: str, poll_timeout: int = 50) -> None:
    telegram.call("deleteWebhook")
    logger.info("bot @%s is polling", telegram.bot_username())
    while True:
        try:
            with db.connect(database_url) as conn:
                state = db.get_state(conn, OFFSET_KEY)
                # Ожидание Telegram длится до минуты: транзакцию не держим, иначе
                # изменения схемы базы ждут бота и всё встаёт в очередь блокировок.
                conn.commit()
                updates = telegram.call(
                    "getUpdates",
                    http_timeout=poll_timeout + 10,
                    offset=state.last_id if state else 0,
                    timeout=poll_timeout,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    try:
                        _update(conn, update)
                    except Exception:
                        # Одно «плохое» сообщение не должно ронять бота по кругу.
                        conn.rollback()
                        logger.exception("update %s failed", update.get("update_id"))
                    # Сдвигаем offset после каждого апдейта: упадём — не ответим дважды.
                    db.save_state(
                        conn, OFFSET_KEY, db.SyncState(update["update_id"] + 1, None, None)
                    )
                    conn.commit()
        except telegram.TelegramError as error:
            logger.warning("getUpdates failed: %s", error)
            time.sleep(10)
        except psycopg.Error as error:
            # Only the type: database errors can quote the connection string.
            logger.warning("database error: %s", type(error).__name__)
            time.sleep(10)
